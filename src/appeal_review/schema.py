"""SQLite 表结构。

设计要点：

- 证据（documents）与补证提交（supplement_submissions）均为追加式，
  新版本只能插入、不能覆盖旧版本；``AFTER UPDATE`` 触发器禁止关键字段变更。
- 审计日志（audit_log）由触发器在所有写操作时自动追加，应用层无法删除或改写。
- 案件状态以单行 ``cases`` 表为准，状态迁移全部在事务内完成。
"""
from __future__ import annotations

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    role        TEXT NOT NULL CHECK (role IN ('institution','secretary','expert','admin')),
    name        TEXT NOT NULL,
    institution_id TEXT,
    token       TEXT UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 一所院校（机构）账户下可以有多名用户，共享 institution_id 的案件可见范围。
CREATE TABLE IF NOT EXISTS cases (
    id              TEXT PRIMARY KEY,
    institution_id  TEXT NOT NULL,
    title           TEXT NOT NULL,
    category        TEXT,
    state           TEXT NOT NULL CHECK (state IN
                        ('submitted','accepted','supplementing','reviewing','decided',
                         'merged','withdrawn','reopened')),
    merged_into     TEXT REFERENCES cases(id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    accepted_at     TEXT,
    accept_due      TEXT,          -- 受理期限
    decided_at      TEXT,
    closed          INTEGER NOT NULL DEFAULT 0  -- 终态标记（决定/撤回/并入他案）
);
CREATE INDEX IF NOT EXISTS idx_cases_institution ON cases(institution_id);
CREATE INDEX IF NOT EXISTS idx_cases_state ON cases(state);

-- 幂等键：写操作按 (操作类型, 客户键) 去重，安全重试。
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idempotency_key TEXT NOT NULL,
    operation       TEXT NOT NULL,
    user_id         TEXT NOT NULL,
    response_body   TEXT NOT NULL,   -- 首次成功调用的响应，重试时原样回放
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (operation, idempotency_key)
) WITHOUT ROWID;

-- 补证轮次：每发一次补证通知开启一轮，院校在期限内提交即关闭。
CREATE TABLE IF NOT EXISTS supplement_rounds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL REFERENCES cases(id),
    reason      TEXT NOT NULL,
    due_at      TEXT NOT NULL,
    opened_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    submitted_at TEXT,
    late        INTEGER NOT NULL DEFAULT 0,
    UNIQUE(case_id, id)
);
CREATE INDEX IF NOT EXISTS idx_rounds_case ON supplement_rounds(case_id);

-- 证据文档：追加式版本链。late=1 的逾期材料永远不会成为当前有效版本。
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL REFERENCES cases(id),
    round_id    INTEGER REFERENCES supplement_rounds(id),
    version     INTEGER NOT NULL,
    filename    TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    size_bytes  INTEGER NOT NULL CHECK (size_bytes >= 0),
    submitted_by TEXT NOT NULL REFERENCES users(id),
    submitted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    late        INTEGER NOT NULL DEFAULT 0,
    current     INTEGER NOT NULL DEFAULT 1,   -- 当前有效版本
    note        TEXT NOT NULL DEFAULT '',
    UNIQUE(case_id, version)
);
CREATE INDEX IF NOT EXISTS idx_documents_case ON documents(case_id);

-- 回避关系：院校申报或秘书登记，凡命中的专家不得进入复核组。
CREATE TABLE IF NOT EXISTS recusals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL REFERENCES cases(id),
    expert_id   TEXT NOT NULL REFERENCES users(id),
    reason      TEXT NOT NULL,
    declared_by TEXT NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(case_id, expert_id)
);

-- 复核组成员；原评审人（is_original_reviewer=1）在规则上必须退出。
CREATE TABLE IF NOT EXISTS panel_assignments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL REFERENCES cases(id),
    expert_id   TEXT NOT NULL REFERENCES users(id),
    is_original_reviewer INTEGER NOT NULL DEFAULT 0,
    assigned_by TEXT NOT NULL REFERENCES users(id),
    removed     INTEGER NOT NULL DEFAULT 0,
    assigned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    removed_at  TEXT,
    UNIQUE(case_id, expert_id)
);

-- 法定签署：一位专家对一个决定草案至多一条有效签署。
CREATE TABLE IF NOT EXISTS signatures (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL REFERENCES cases(id),
    expert_id   TEXT NOT NULL REFERENCES users(id),
    signed_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    revoked     INTEGER NOT NULL DEFAULT 0,
    revoked_at  TEXT,
    UNIQUE(case_id, expert_id)
);

-- 暂缓执行措施：决定作出前后均可挂起，决定措施类型与解除需权限控制。
CREATE TABLE IF NOT EXISTS stays (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL REFERENCES cases(id),
    reason      TEXT NOT NULL,
    granted_by  TEXT NOT NULL REFERENCES users(id),
    granted_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    lifted_by   TEXT REFERENCES users(id),
    lifted_at   TEXT,
    active      INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_stays_case ON stays(case_id);

-- 案件合并/重开历史。
CREATE TABLE IF NOT EXISTS case_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id    TEXT NOT NULL REFERENCES cases(id),
    parent_id   TEXT NOT NULL REFERENCES cases(id),
    kind        TEXT NOT NULL CHECK (kind IN ('merge','reopen')),
    created_by  TEXT NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- 撤回申请：院校提出、秘书核准后案件才进入 withdrawn，权限分离。
CREATE TABLE IF NOT EXISTS withdrawal_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL REFERENCES cases(id),
    reason      TEXT NOT NULL,
    requested_by TEXT NOT NULL REFERENCES users(id),
    requested_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    decided_by  TEXT REFERENCES users(id),
    decided_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_withdrawal_case ON withdrawal_requests(case_id);

-- 决定：终局决定与因重开产生的新决定逐版保存，差异比对基于此。
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id     TEXT NOT NULL REFERENCES cases(id),
    version     INTEGER NOT NULL,
    outcome     TEXT NOT NULL CHECK (outcome IN ('upheld','modified','revoked','remanded')),
    rationale   TEXT NOT NULL,
    signed_by   TEXT NOT NULL,           -- 签署专家 id 的 JSON 数组
    created_by  TEXT NOT NULL REFERENCES users(id),
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(case_id, version)
);

-- 只增审计日志：应用只插入；触发器兜底记录 UPDATE/DELETE。
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    actor_id    TEXT,
    action      TEXT NOT NULL,
    entity      TEXT NOT NULL,
    entity_id   TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- 关键业务记录只准插入、不准改写/删除。
CREATE TRIGGER IF NOT EXISTS trg_documents_immutable
BEFORE UPDATE OF case_id, version, content_hash, submitted_at, late ON documents
BEGIN
    SELECT RAISE(ABORT, 'documents are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_documents_nodelete
BEFORE DELETE ON documents
BEGIN
    SELECT RAISE(ABORT, 'documents are append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_audit_immutable
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_audit_nodelete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS trg_decisions_immutable
BEFORE UPDATE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions are append-only');
END;
"""


def initialize(conn) -> None:
    """在给定连接上创建全部表与触发器（可重复执行）。"""
    conn.executescript(SCHEMA)
    conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
