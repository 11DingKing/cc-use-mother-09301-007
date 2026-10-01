"""SQLite 存储层：表结构、连接管理与原子写入原语。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('institution','secretary','expert','admin')),
    institution_id INTEGER
);

CREATE TABLE IF NOT EXISTS institutions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY,
    case_no TEXT NOT NULL UNIQUE,
    institution_id INTEGER NOT NULL REFERENCES institutions(id),
    subject TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'submitted','accepted','supplementing','reviewing','decided',
        'merged','withdrawn','reopened'
    )),
    parent_case_id INTEGER REFERENCES cases(id),
    supersedes_case_id INTEGER REFERENCES cases(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS deadlines (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    kind TEXT NOT NULL,
    due_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    institution_id INTEGER NOT NULL REFERENCES institutions(id),
    version_no INTEGER NOT NULL,
    title TEXT NOT NULL,
    content_ref TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    on_time INTEGER NOT NULL,
    accepted_as_new_version INTEGER NOT NULL DEFAULT 1,
    late_reason TEXT
);

CREATE TABLE IF NOT EXISTS recusals (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    expert_id INTEGER NOT NULL REFERENCES users(id),
    reason TEXT NOT NULL,
    declared_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (case_id, expert_id)
);

CREATE TABLE IF NOT EXISTS reviewers (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    expert_id INTEGER NOT NULL REFERENCES users(id),
    assigned_at TEXT NOT NULL,
    UNIQUE (case_id, expert_id)
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL UNIQUE REFERENCES cases(id),
    ruling TEXT NOT NULL CHECK (ruling IN ('uphold','modify','revoke')),
    body TEXT NOT NULL,
    differs_from_original INTEGER NOT NULL,
    original_outcome TEXT,
    signed_count INTEGER NOT NULL,
    required_signatures INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS signatures (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    expert_id INTEGER NOT NULL REFERENCES users(id),
    signed_at TEXT NOT NULL,
    UNIQUE (decision_id, expert_id)
);

CREATE TABLE IF NOT EXISTS case_actions (
    id INTEGER PRIMARY KEY,
    case_id INTEGER,
    action TEXT NOT NULL,
    actor_id INTEGER,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stays (
    id INTEGER PRIMARY KEY,
    case_id INTEGER NOT NULL REFERENCES cases(id),
    active INTEGER NOT NULL DEFAULT 1,
    reason TEXT NOT NULL,
    granted_by INTEGER NOT NULL REFERENCES users(id),
    granted_at TEXT NOT NULL,
    lifted_by INTEGER REFERENCES users(id),
    lifted_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

-- 幂等键：同一主体 + 键只能对应一次写入；重放时返回首次结果快照。
CREATE TABLE IF NOT EXISTS idempotency (
    id INTEGER PRIMARY KEY,
    actor_id INTEGER NOT NULL,
    idem_key TEXT NOT NULL,
    scope TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_code INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (actor_id, idem_key)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def open_db(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即开始的写事务；提交/回滚由上下文统一处理，可安全重试。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def audit(conn: sqlite3.Connection, case_id: int | None, action: str,
          actor_id: int | None, detail: str) -> None:
    conn.execute(
        "INSERT INTO case_actions (case_id, action, actor_id, detail, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (case_id, action, actor_id, detail, utcnow()),
    )
