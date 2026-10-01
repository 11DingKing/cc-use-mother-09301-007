"""领域服务：受理期限、证据版本、利益回避、法定签署、暂缓执行与案件流转。

所有写操作都在 ``BEGIN IMMEDIATE`` 事务内执行，并通过幂等键支持安全重试：
相同主体使用相同键重放时返回首次结果，请求体不一致则拒绝。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .errors import (
    ConflictError,
    NotFoundError,
    PermissionError,
    QuorumError,
    StateConflictError,
    ValidationError,
)
from .store import audit, open_db, transaction, utcnow

OPEN_STATUSES = {"submitted", "accepted", "supplementing", "reviewing", "reopened"}
TERMINAL_STATUSES = {"decided", "withdrawn", "merged"}

STATE_LABELS = {
    "submitted": "提交",
    "accepted": "受理",
    "supplementing": "补证",
    "reviewing": "复核",
    "decided": "决定",
    "merged": "已合并",
    "withdrawn": "已撤回",
    "reopened": "重开",
}

RULING_LABELS = {"uphold": "维持", "modify": "变更", "revoke": "撤销"}


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


class AppealService:
    def __init__(
        self,
        db_path: str = ":memory:",
        *,
        supplement_days: int = 10,
        required_signatures: int = 3,
    ) -> None:
        self.supplement_days = supplement_days
        self.required_signatures = required_signatures
        # sqlite3 连接不能跨线程并发使用；全局锁串行化所有访问，
        # BEGIN IMMEDIATE 再保证跨进程的写入安全重试。
        self._lock = threading.RLock()
        self.conn = open_db(db_path)
        self._seed()

    # ------------------------------------------------------------------ 基础

    def _seed(self) -> None:
        with transaction(self.conn):
            rows = self.conn.execute("SELECT COUNT(*) AS n FROM institutions").fetchone()
            if rows["n"]:
                return
            self.conn.executemany(
                "INSERT INTO institutions (id, name) VALUES (?, ?)",
                [(1, "地方院校甲"), (2, "地方院校乙")],
            )
            users = [
                ("school_a", "甲校申诉专员", "institution", 1),
                ("school_b", "乙校申诉专员", "institution", 2),
                ("sec", "复核秘书", "secretary", None),
                ("expert1", "独立专家一", "expert", None),
                ("expert2", "独立专家二", "expert", None),
                ("expert3", "独立专家三", "expert", None),
                ("expert4", "独立专家四", "expert", None),
                ("orig", "原评审人", "expert", None),
                ("admin", "审计管理员", "admin", None),
            ]
            self.conn.executemany(
                "INSERT INTO users (username, display_name, role, institution_id)"
                " VALUES (?, ?, ?, ?)",
                users,
            )

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    @contextmanager
    def guard(self):
        """串行化对共享 SQLite 连接的访问（HTTP 多线程入口）。"""
        with self._lock:
            yield

    def login(self, username: str) -> dict[str, Any]:
        with transaction(self.conn):
            user = self.conn.execute(
                "SELECT * FROM users WHERE username=?", (username,)
            ).fetchone()
            if user is None:
                raise NotFoundError("用户不存在")
            token = secrets.token_urlsafe(24)
            self.conn.execute(
                "INSERT INTO sessions (token, user_id, created_at) VALUES (?, ?, ?)",
                (token, user["id"], utcnow()),
            )
            return {"token": token, "user": self._user_view(user)}

    def authenticate(self, token: str | None) -> sqlite3.Row:
        if not token:
            raise PermissionError("缺少登录令牌")
        row = self.conn.execute(
            "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id"
            " WHERE s.token=?",
            (token,),
        ).fetchone()
        if row is None:
            raise PermissionError("令牌无效或已过期")
        return row

    def _user_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "username": row["username"],
            "display_name": row["display_name"],
            "role": row["role"],
            "institution_id": row["institution_id"],
        }

    def _require_role(self, user: sqlite3.Row, *roles: str) -> None:
        if user["role"] not in roles:
            raise PermissionError(f"需要角色：{'、'.join(roles)}")

    def _get_case(self, conn: sqlite3.Connection, ref: str | int) -> sqlite3.Row:
        if isinstance(ref, int) or str(ref).isdigit():
            row = conn.execute("SELECT * FROM cases WHERE id=?", (int(ref),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM cases WHERE case_no=?", (ref,)).fetchone()
        if row is None:
            raise NotFoundError(f"案件不存在：{ref}")
        return row

    def _idempotent(
        self,
        actor_id: int,
        key: str | None,
        scope: str,
        payload: dict[str, Any],
        fn: Callable[[sqlite3.Connection], tuple[int, dict[str, Any]]],
    ) -> tuple[int, dict[str, Any]]:
        """在写事务内执行 fn，并通过幂等键保证可安全重试。"""
        if not key:
            with transaction(self.conn) as conn:
                return fn(conn)
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with transaction(self.conn) as conn:
            existing = conn.execute(
                "SELECT * FROM idempotency WHERE actor_id=? AND idem_key=?",
                (actor_id, key),
            ).fetchone()
            if existing is not None:
                if existing["request_hash"] != digest:
                    raise ConflictError(
                        "幂等键已被不同请求使用", code="IDEMPOTENCY_KEY_REUSED"
                    )
                return existing["response_code"], json.loads(existing["response_body"])
            code, body = fn(conn)
            conn.execute(
                "INSERT INTO idempotency (actor_id, idem_key, scope, request_hash,"
                " response_code, response_body, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (actor_id, key, scope, digest, code,
                 json.dumps(body, ensure_ascii=False), utcnow()),
            )
            return code, body

    # ------------------------------------------------------------------ 案件

    def create_case(self, user: sqlite3.Row, payload: dict[str, Any],
                    idem_key: str | None) -> tuple[int, dict[str, Any]]:
        self._require_role(user, "institution")
        subject = (payload.get("subject") or "").strip()
        if not subject:
            raise ValidationError("subject 不能为空")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            inst_id = user["institution_id"]
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM cases WHERE institution_id=?", (inst_id,)
            ).fetchone()["n"]
            case_no = payload.get("case_no") or f"{inst_id:05d}-{count + 1:03d}-A"
            try:
                cur = conn.execute(
                    "INSERT INTO cases (case_no, institution_id, subject, status,"
                    " created_at, updated_at) VALUES (?, ?, ?, 'submitted', ?, ?)",
                    (case_no, inst_id, subject, utcnow(), utcnow()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError(f"案件编号已存在：{case_no}")
            case_id = cur.lastrowid
            audit(conn, case_id, "submit", user["id"],
                  f"院校提交申诉：{subject}")
            return 201, self._case_view(conn, conn.execute(
                "SELECT * FROM cases WHERE id=?", (case_id,)).fetchone())

        return self._idempotent(user["id"], idem_key, "create_case", payload, op)

    def accept_case(self, user: sqlite3.Row, case_ref: str,
                    idem_key: str | None) -> tuple[int, dict[str, Any]]:
        self._require_role(user, "secretary", "admin")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            case = self._get_case(conn, case_ref)
            if case["status"] != "submitted":
                raise StateConflictError(
                    f"当前状态「{STATE_LABELS[case['status']]}」不能受理，仅提交状态可受理"
                )
            conn.execute(
                "UPDATE cases SET status='accepted', updated_at=?, row_version=row_version+1"
                " WHERE id=?",
                (utcnow(), case["id"]),
            )
            audit(conn, case["id"], "accept", user["id"], "申诉已受理，进入复核排期")
            return 200, self._case_view(conn, self._get_case(conn, case["id"]))

        return self._idempotent(user["id"], idem_key, "accept_case",
                                {"case": str(case_ref)}, op)

    def request_supplement(self, user: sqlite3.Row, case_ref: str,
                           payload: dict[str, Any],
                           idem_key: str | None) -> tuple[int, dict[str, Any]]:
        self._require_role(user, "secretary")
        days = int(payload.get("days", self.supplement_days))
        if days <= 0:
            raise ValidationError("补证期限必须为正整数（天）")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            case = self._get_case(conn, case_ref)
            if case["status"] not in ("accepted", "reopened", "reviewing"):
                raise StateConflictError(
                    f"当前状态「{STATE_LABELS[case['status']]}」不能要求补证"
                )
            due = self.now() + timedelta(days=days)
            conn.execute(
                "UPDATE cases SET status='supplementing', updated_at=?,"
                " row_version=row_version+1 WHERE id=?",
                (utcnow(), case["id"]),
            )
            cur = conn.execute(
                "INSERT INTO deadlines (case_id, kind, due_at, created_at)"
                " VALUES (?, 'supplement', ?, ?)",
                (case["id"], due.isoformat(timespec="seconds"), utcnow()),
            )
            audit(conn, case["id"], "supplement_requested", user["id"],
                  f"要求补证，截止 {due.isoformat(timespec='seconds')}（deadline#{cur.lastrowid}）")
            return 201, self._case_view(conn, self._get_case(conn, case["id"]))

        return self._idempotent(user["id"], idem_key, "supplement",
                                {"case": str(case_ref), "days": days}, op)

    # -------------------------------------------------------------- 证据版本

    def submit_evidence(self, user: sqlite3.Row, case_ref: str,
                        payload: dict[str, Any],
                        idem_key: str | None) -> tuple[int, dict[str, Any]]:
        self._require_role(user, "institution")
        title = (payload.get("title") or "").strip()
        content_ref = (payload.get("content_ref") or "").strip()
        if not title or not content_ref:
            raise ValidationError("title 与 content_ref 不能为空")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            case = self._get_case(conn, case_ref)
            if case["institution_id"] != user["institution_id"]:
                raise PermissionError("只能为所属院校的案件提交证据")
            if case["status"] in TERMINAL_STATUSES:
                raise StateConflictError(
                    f"案件已处于「{STATE_LABELS[case['status']]}」，不能再提交证据"
                )
            deadline = conn.execute(
                "SELECT * FROM deadlines WHERE case_id=? AND kind='supplement'"
                " ORDER BY due_at DESC LIMIT 1",
                (case["id"],),
            ).fetchone()
            on_time = 1
            late_reason = None
            now = self.now()
            if deadline is not None:
                due = _parse(deadline["due_at"])
                if now > due:
                    on_time = 0
                    late_reason = f"超过补证期限 {deadline['due_at']}，作为新版本留存"
                    audit(conn, case["id"], "evidence_late", user["id"],
                          f"逾期材料进入新版本（截止 {deadline['due_at']}）")
            version_no = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) + 1 AS v FROM evidence"
                " WHERE case_id=?",
                (case["id"],),
            ).fetchone()["v"]
            cur = conn.execute(
                "INSERT INTO evidence (case_id, institution_id, version_no, title,"
                " content_ref, submitted_at, on_time, accepted_as_new_version,"
                " late_reason) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                (case["id"], user["institution_id"], version_no, title, content_ref,
                 now.isoformat(timespec="seconds"), on_time, late_reason),
            )
            conn.execute(
                "UPDATE cases SET updated_at=? WHERE id=?", (utcnow(), case["id"])
            )
            audit(conn, case["id"], "evidence_submitted", user["id"],
                  f"证据版本 v{version_no}：{title}"
                  + ("（逾期，仅入新版本）" if not on_time else ""))
            row = conn.execute("SELECT * FROM evidence WHERE id=?",
                               (cur.lastrowid,)).fetchone()
            return 201, self._evidence_view(row)

        return self._idempotent(user["id"], idem_key, "evidence",
                                {"case": str(case_ref), **payload}, op)

    # ---------------------------------------------------------------- 回避

    def declare_recusal(self, user: sqlite3.Row, case_ref: str,
                        payload: dict[str, Any],
                        idem_key: str | None) -> tuple[int, dict[str, Any]]:
        """登记回避关系。秘书可代登记，专家可自行声明；原评审人必须退出复核。"""
        self._require_role(user, "secretary", "admin", "expert")
        expert_ref = payload.get("expert_id") or payload.get("expert_username")
        reason = (payload.get("reason") or "").strip()
        if not expert_ref or not reason:
            raise ValidationError("expert_id（或 expert_username）与 reason 不能为空")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            case = self._get_case(conn, case_ref)
            expert = self._resolve_expert(conn, expert_ref)
            if user["role"] == "expert" and expert["id"] != user["id"]:
                raise PermissionError("专家只能声明本人的回避")
            try:
                cur = conn.execute(
                    "INSERT INTO recusals (case_id, expert_id, reason, declared_by,"
                    " created_at) VALUES (?, ?, ?, ?, ?)",
                    (case["id"], expert["id"], reason,
                     f"{user['role']}:{user['id']}", utcnow()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("该专家对本案的回避关系已登记")
            conn.execute("DELETE FROM reviewers WHERE case_id=? AND expert_id=?",
                         (case["id"], expert["id"]))
            audit(conn, case["id"], "recusal_declared", user["id"],
                  f"专家 {expert['display_name']} 回避：{reason}；已移出复核名单")
            row = conn.execute("SELECT * FROM recusals WHERE id=?",
                               (cur.lastrowid,)).fetchone()
            return 201, self._recusal_view(conn, row)

        return self._idempotent(user["id"], idem_key, "recusal",
                                {"case": str(case_ref), **payload}, op)

    def _resolve_expert(self, conn: sqlite3.Connection, ref: Any) -> sqlite3.Row:
        if isinstance(ref, int) or str(ref).isdigit():
            row = conn.execute(
                "SELECT * FROM users WHERE id=? AND role='expert'", (int(ref),)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM users WHERE username=? AND role='expert'", (str(ref),)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"专家不存在：{ref}")
        return row

    def assign_reviewer(self, user: sqlite3.Row, case_ref: str,
                        payload: dict[str, Any],
                        idem_key: str | None) -> tuple[int, dict[str, Any]]:
        self._require_role(user, "secretary")
        expert_ref = payload.get("expert_id") or payload.get("expert_username")
        if not expert_ref:
            raise ValidationError("expert_id 或 expert_username 不能为空")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            case = self._get_case(conn, case_ref)
            expert = self._resolve_expert(conn, expert_ref)
            blocked = conn.execute(
                "SELECT 1 FROM recusals WHERE case_id=? AND expert_id=?",
                (case["id"], expert["id"]),
            ).fetchone()
            if blocked is not None:
                raise PermissionError(
                    f"专家 {expert['display_name']} 对本案存在回避关系，不得参与复核"
                )
            try:
                conn.execute(
                    "INSERT INTO reviewers (case_id, expert_id, assigned_at)"
                    " VALUES (?, ?, ?)",
                    (case["id"], expert["id"], utcnow()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("该专家已在复核名单中")
            if case["status"] == "accepted":
                conn.execute(
                    "UPDATE cases SET status='reviewing', updated_at=?,"
                    " row_version=row_version+1 WHERE id=?",
                    (utcnow(), case["id"]),
                )
            audit(conn, case["id"], "reviewer_assigned", user["id"],
                  f"指派复核专家 {expert['display_name']}")
            return 201, self._case_view(conn, self._get_case(conn, case["id"]))

        return self._idempotent(user["id"], idem_key, "assign_reviewer",
                                {"case": str(case_ref), **payload}, op)

    # ---------------------------------------------------------------- 决定

    def create_decision(self, user: sqlite3.Row, case_ref: str,
                        payload: dict[str, Any],
                        idem_key: str | None) -> tuple[int, dict[str, Any]]:
        """起草复核决定。签署人必须是无回避关系的在案专家，且满足法定人数。"""
        self._require_role(user, "secretary", "admin")
        ruling = payload.get("ruling")
        body = (payload.get("body") or "").strip()
        if ruling not in RULING_LABELS or not body:
            raise ValidationError("ruling 必须是 uphold/modify/revoke，且 body 不能为空")
        signer_refs = payload.get("signer_ids") or []
        if not isinstance(signer_refs, list) or not signer_refs:
            raise ValidationError("signer_ids 必须是非空数组")
        if len(set(map(str, signer_refs))) != len(signer_refs):
            raise ValidationError("签署人不能重复")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            case = self._get_case(conn, case_ref)
            if case["status"] in ("decided", "withdrawn", "merged"):
                raise StateConflictError(
                    f"案件已「{STATE_LABELS[case['status']]}」，不能作出决定"
                )
            if len(signer_refs) < self.required_signatures:
                raise QuorumError(
                    f"法定签署人数为 {self.required_signatures} 人，"
                    f"当前仅 {len(signer_refs)} 人"
                )
            signer_ids: list[int] = []
            for ref in signer_refs:
                expert = self._resolve_expert(conn, ref)
                recused = conn.execute(
                    "SELECT 1 FROM recusals WHERE case_id=? AND expert_id=?",
                    (case["id"], expert["id"]),
                ).fetchone()
                if recused is not None:
                    raise PermissionError(
                        f"签署人 {expert['display_name']} 存在回避关系，"
                        "原评审人不得参与复核决定"
                    )
                assigned = conn.execute(
                    "SELECT 1 FROM reviewers WHERE case_id=? AND expert_id=?",
                    (case["id"], expert["id"]),
                ).fetchone()
                if assigned is None:
                    raise PermissionError(
                        f"签署人 {expert['display_name']} 不在本案复核名单中"
                    )
                signer_ids.append(expert["id"])
            differs = ruling in ("modify", "revoke")
            cur = conn.execute(
                "INSERT INTO decisions (case_id, ruling, body, differs_from_original,"
                " original_outcome, signed_count, required_signatures, created_at)"
                " VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (case["id"], ruling, body, int(differs),
                 payload.get("original_outcome"), self.required_signatures, utcnow()),
            )
            decision_id = cur.lastrowid
            for expert_id in signer_ids:
                conn.execute(
                    "INSERT INTO signatures (decision_id, expert_id, signed_at)"
                    " VALUES (?, ?, ?)",
                    (decision_id, expert_id, utcnow()),
                )
            signed_count = len(signer_ids)
            conn.execute(
                "UPDATE decisions SET signed_count=? WHERE id=?",
                (signed_count, decision_id),
            )
            conn.execute(
                "UPDATE cases SET status='decided', updated_at=?,"
                " row_version=row_version+1 WHERE id=?",
                (utcnow(), case["id"]),
            )
            audit(conn, case["id"], "decision_issued", user["id"],
                  f"决定「{RULING_LABELS[ruling]}」由 {signed_count}/"
                  f"{self.required_signatures} 名法定专家签署"
                  + ("，与原评审结果存在差异" if differs else "，维持原结果"))
            return 201, self._decision_view(
                conn, conn.execute("SELECT * FROM decisions WHERE id=?",
                                   (decision_id,)).fetchone())

        return self._idempotent(user["id"], idem_key, "decision",
                                {"case": str(case_ref), **payload}, op)

    def sign_decision(self, user: sqlite3.Row, case_ref: str,
                      idem_key: str | None) -> tuple[int, dict[str, Any]]:
        """专家追加签署（允许签署人数在起草后继续补齐至法定人数）。"""
        self._require_role(user, "expert")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            case = self._get_case(conn, case_ref)
            decision = conn.execute(
                "SELECT * FROM decisions WHERE case_id=?", (case["id"],)
            ).fetchone()
            if decision is None:
                raise NotFoundError("本案尚未起草决定")
            recused = conn.execute(
                "SELECT 1 FROM recusals WHERE case_id=? AND expert_id=?",
                (case["id"], user["id"]),
            ).fetchone()
            if recused is not None:
                raise PermissionError("存在回避关系，不能签署")
            assigned = conn.execute(
                "SELECT 1 FROM reviewers WHERE case_id=? AND expert_id=?",
                (case["id"], user["id"]),
            ).fetchone()
            if assigned is None:
                raise PermissionError("不在本案复核名单中，不能签署")
            try:
                conn.execute(
                    "INSERT INTO signatures (decision_id, expert_id, signed_at)"
                    " VALUES (?, ?, ?)",
                    (decision["id"], user["id"], utcnow()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("已签署，请勿重复签署")
            count = conn.execute(
                "SELECT COUNT(*) AS n FROM signatures WHERE decision_id=?",
                (decision["id"],),
            ).fetchone()["n"]
            conn.execute("UPDATE decisions SET signed_count=? WHERE id=?",
                         (count, decision["id"]))
            audit(conn, case["id"], "decision_signed", user["id"],
                  f"专家完成签署，当前 {count}/{decision['required_signatures']}")
            if count >= decision["required_signatures"] and case["status"] != "decided":
                conn.execute(
                    "UPDATE cases SET status='decided', updated_at=? WHERE id=?",
                    (utcnow(), case["id"]),
                )
            return 200, self._decision_view(
                conn, conn.execute("SELECT * FROM decisions WHERE id=?",
                                   (decision["id"],)).fetchone())

        return self._idempotent(user["id"], idem_key, "sign",
                                {"case": str(case_ref)}, op)

    # ------------------------------------------------------------ 暂缓执行

    def grant_stay(self, user: sqlite3.Row, case_ref: str,
                   payload: dict[str, Any],
                   idem_key: str | None) -> tuple[int, dict[str, Any]]:
        self._require_role(user, "secretary", "admin")
        reason = (payload.get("reason") or "").strip()
        if not reason:
            raise ValidationError("reason 不能为空")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            case = self._get_case(conn, case_ref)
            if case["status"] == "withdrawn":
                raise StateConflictError("已撤回案件不能采取暂缓措施")
            active = conn.execute(
                "SELECT 1 FROM stays WHERE case_id=? AND active=1", (case["id"],)
            ).fetchone()
            if active is not None:
                raise ConflictError("本案已有生效中的暂缓执行措施")
            cur = conn.execute(
                "INSERT INTO stays (case_id, active, reason, granted_by, granted_at)"
                " VALUES (?, 1, ?, ?, ?)",
                (case["id"], reason, user["id"], utcnow()),
            )
            audit(conn, case["id"], "stay_granted", user["id"], f"暂缓执行：{reason}")
            return 201, self._stay_view(
                conn, conn.execute("SELECT * FROM stays WHERE id=?",
                                   (cur.lastrowid,)).fetchone())

        return self._idempotent(user["id"], idem_key, "stay_grant",
                                {"case": str(case_ref), **payload}, op)

    def lift_stay(self, user: sqlite3.Row, case_ref: str,
                  idem_key: str | None) -> tuple[int, dict[str, Any]]:
        self._require_role(user, "secretary", "admin")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            case = self._get_case(conn, case_ref)
            stay = conn.execute(
                "SELECT * FROM stays WHERE case_id=? AND active=1 ORDER BY id DESC"
                " LIMIT 1",
                (case["id"],),
            ).fetchone()
            if stay is None:
                raise NotFoundError("本案没有生效中的暂缓措施")
            conn.execute(
                "UPDATE stays SET active=0, lifted_by=?, lifted_at=? WHERE id=?",
                (user["id"], utcnow(), stay["id"]),
            )
            audit(conn, case["id"], "stay_lifted", user["id"], "暂缓执行措施解除")
            return 200, self._stay_view(
                conn, conn.execute("SELECT * FROM stays WHERE id=?",
                                   (stay["id"],)).fetchone())

        return self._idempotent(user["id"], idem_key, "stay_lift",
                                {"case": str(case_ref)}, op)

    # ------------------------------------------------------ 合并/撤回/重开

    def merge_cases(self, user: sqlite3.Row, payload: dict[str, Any],
                    idem_key: str | None) -> tuple[int, dict[str, Any]]:
        self._require_role(user, "secretary", "admin")
        source_ref = payload.get("source_case")
        target_ref = payload.get("target_case")
        if not source_ref or not target_ref or str(source_ref) == str(target_ref):
            raise ValidationError("source_case 与 target_case 必须是两个不同案件")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            source = self._get_case(conn, source_ref)
            target = self._get_case(conn, target_ref)
            if source["institution_id"] != target["institution_id"]:
                raise PermissionError("仅能合并同一院校的申诉案件")
            for case in (source, target):
                if case["status"] not in OPEN_STATUSES:
                    raise StateConflictError(
                        f"案件 {case['case_no']} 已「{STATE_LABELS[case['status']]}」，"
                        "不能合并"
                    )
            conn.execute(
                "UPDATE cases SET status='merged', parent_case_id=?, updated_at=?,"
                " row_version=row_version+1 WHERE id=?",
                (target["id"], utcnow(), source["id"]),
            )
            for table, column in (("evidence", "case_id"), ("deadlines", "case_id"),
                                  ("reviewers", "case_id"), ("recusals", "case_id"),
                                  ("stays", "case_id")):
                # reviewers/recusals 与目标案件可能存在同专家唯一约束，冲突行丢弃
                ignore = " OR IGNORE" if table in ("reviewers", "recusals") else ""
                conn.execute(
                    f"UPDATE{ignore} {table} SET {column}=? WHERE {column}=?",
                    (target["id"], source["id"]),
                )
            # 并入材料按提交时间重新编号，保证目标案件版本号连续不冲突
            for seq, ev_id in enumerate(
                (row["id"] for row in conn.execute(
                    "SELECT id FROM evidence WHERE case_id=? ORDER BY submitted_at, id",
                    (target["id"],))), start=1):
                conn.execute("UPDATE evidence SET version_no=? WHERE id=?",
                             (seq, ev_id))
            audit(conn, source["id"], "merged_away", user["id"],
                  f"并入案件 {target['case_no']}")
            audit(conn, target["id"], "merged_into", user["id"],
                  f"吸收案件 {source['case_no']}（材料与期限随之并入）")
            return 200, {
                "source": self._case_view(conn, self._get_case(conn, source["id"])),
                "target": self._case_view(conn, self._get_case(conn, target["id"])),
            }

        return self._idempotent(user["id"], idem_key, "merge", payload, op)

    def withdraw_case(self, user: sqlite3.Row, case_ref: str,
                      payload: dict[str, Any],
                      idem_key: str | None) -> tuple[int, dict[str, Any]]:
        reason = (payload.get("reason") or "").strip()
        if not reason:
            raise ValidationError("reason 不能为空")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            case = self._get_case(conn, case_ref)
            if user["role"] == "institution":
                if case["institution_id"] != user["institution_id"]:
                    raise PermissionError("只能撤回所属院校的案件")
            elif user["role"] not in ("secretary", "admin"):
                raise PermissionError("院校、秘书或管理员才可撤回案件")
            if case["status"] in ("decided", "withdrawn", "merged"):
                raise StateConflictError(
                    f"案件已「{STATE_LABELS[case['status']]}」，不能撤回"
                )
            conn.execute(
                "UPDATE cases SET status='withdrawn', updated_at=?,"
                " row_version=row_version+1 WHERE id=?",
                (utcnow(), case["id"]),
            )
            audit(conn, case["id"], "withdrawn", user["id"], f"撤回申诉：{reason}")
            return 200, self._case_view(conn, self._get_case(conn, case["id"]))

        return self._idempotent(user["id"], idem_key, "withdraw",
                                {"case": str(case_ref), **payload}, op)

    def reopen_case(self, user: sqlite3.Row, case_ref: str,
                    payload: dict[str, Any],
                    idem_key: str | None) -> tuple[int, dict[str, Any]]:
        """重开属于高权限操作：仅管理员可推进，并全程留痕。"""
        self._require_role(user, "admin")
        reason = (payload.get("reason") or "").strip()
        if not reason:
            raise ValidationError("reason 不能为空")

        def op(conn: sqlite3.Connection) -> tuple[int, dict[str, Any]]:
            case = self._get_case(conn, case_ref)
            if case["status"] not in ("withdrawn", "merged"):
                raise StateConflictError(
                    "只有已撤回或已合并的案件才能重开"
                )
            conn.execute(
                "UPDATE cases SET status='reopened', parent_case_id=NULL,"
                " updated_at=?, row_version=row_version+1 WHERE id=?",
                (utcnow(), case["id"]),
            )
            audit(conn, case["id"], "reopened", user["id"],
                  f"管理员批准重开：{reason}")
            return 200, self._case_view(conn, self._get_case(conn, case["id"]))

        return self._idempotent(user["id"], idem_key, "reopen",
                                {"case": str(case_ref), **payload}, op)

    # ---------------------------------------------------------------- 查询

    def list_cases(self, user: sqlite3.Row) -> dict[str, Any]:
        if user["role"] == "institution":
            rows = self.conn.execute(
                "SELECT * FROM cases WHERE institution_id=? ORDER BY id",
                (user["institution_id"],),
            ).fetchall()
        elif user["role"] == "expert":
            rows = self.conn.execute(
                "SELECT DISTINCT c.* FROM cases c JOIN reviewers r ON r.case_id=c.id"
                " WHERE r.expert_id=? ORDER BY c.id",
                (user["id"],),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM cases ORDER BY id").fetchall()
        return {"cases": [self._case_view(self.conn, r) for r in rows]}

    def case_detail(self, user: sqlite3.Row, case_ref: str) -> dict[str, Any]:
        case = self._get_case(self.conn, case_ref)
        if user["role"] == "institution" and \
                case["institution_id"] != user["institution_id"]:
            # 不暴露其他院校材料的存在
            raise NotFoundError(f"案件不存在：{case_ref}")
        if user["role"] == "expert":
            member = self.conn.execute(
                "SELECT 1 FROM reviewers WHERE case_id=? AND expert_id=?",
                (case["id"], user["id"]),
            ).fetchone()
            if member is None:
                raise NotFoundError(f"案件不存在：{case_ref}")
        return self._case_view(self.conn, case, full=True)

    def own_decisions(self, user: sqlite3.Row) -> dict[str, Any]:
        """院校视角：仅看到本院校决定，以及与原评审结果的差异。"""
        self._require_role(user, "institution")
        rows = self.conn.execute(
            "SELECT d.* FROM decisions d JOIN cases c ON c.id=d.case_id"
            " WHERE c.institution_id=? ORDER BY d.id",
            (user["institution_id"],),
        ).fetchall()
        return {"decisions": [self._decision_view(self.conn, r) for r in rows]}

    def audit_log(self, user: sqlite3.Row, limit: int = 100,
                  offset: int = 0) -> dict[str, Any]:
        """管理员审计：全过程动作留痕，但不返回任何证据正文或材料指针。"""
        self._require_role(user, "admin")
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        rows = self.conn.execute(
            "SELECT a.id, a.case_id, c.case_no, a.action, a.actor_id,"
            " u.display_name AS actor_name, u.role AS actor_role, a.detail,"
            " a.created_at FROM case_actions a"
            " LEFT JOIN cases c ON c.id=a.case_id"
            " LEFT JOIN users u ON u.id=a.actor_id"
            " ORDER BY a.id LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM case_actions"
        ).fetchone()["n"]
        return {
            "total": total,
            "limit": limit,
            "offset": offset,
            "entries": [dict(r) for r in rows],
        }

    # ---------------------------------------------------------------- 视图

    def _evidence_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "version_no": row["version_no"],
            "title": row["title"],
            "content_ref": row["content_ref"],
            "submitted_at": row["submitted_at"],
            "on_time": bool(row["on_time"]),
            "late_reason": row["late_reason"],
        }

    def _recusal_view(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        expert = conn.execute("SELECT * FROM users WHERE id=?",
                              (row["expert_id"],)).fetchone()
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "expert_id": row["expert_id"],
            "expert_name": expert["display_name"],
            "reason": row["reason"],
            "declared_by": row["declared_by"],
            "created_at": row["created_at"],
        }

    def _stay_view(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "active": bool(row["active"]),
            "reason": row["reason"],
            "granted_at": row["granted_at"],
            "lifted_at": row["lifted_at"],
        }

    def _decision_view(self, conn: sqlite3.Connection,
                       row: sqlite3.Row) -> dict[str, Any]:
        signers = conn.execute(
            "SELECT u.id, u.display_name, s.signed_at FROM signatures s"
            " JOIN users u ON u.id=s.expert_id WHERE s.decision_id=?"
            " ORDER BY s.id",
            (row["id"],),
        ).fetchall()
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "ruling": row["ruling"],
            "ruling_label": RULING_LABELS[row["ruling"]],
            "body": row["body"],
            "differs_from_original": bool(row["differs_from_original"]),
            "original_outcome": row["original_outcome"],
            "signed_count": row["signed_count"],
            "required_signatures": row["required_signatures"],
            "quorum_met": row["signed_count"] >= row["required_signatures"],
            "created_at": row["created_at"],
            "signers": [dict(s) for s in signers],
        }

    def _case_view(self, conn: sqlite3.Connection, case: sqlite3.Row,
                   full: bool = False) -> dict[str, Any]:
        view: dict[str, Any] = {
            "id": case["id"],
            "case_no": case["case_no"],
            "institution_id": case["institution_id"],
            "subject": case["subject"],
            "status": case["status"],
            "state": STATE_LABELS[case["status"]],
            "parent_case_id": case["parent_case_id"],
            "updated_at": case["updated_at"],
            "row_version": case["row_version"],
        }
        if not full:
            return view
        deadlines = conn.execute(
            "SELECT id, kind, due_at FROM deadlines WHERE case_id=? ORDER BY id",
            (case["id"],),
        ).fetchall()
        view["deadlines"] = [dict(d) for d in deadlines]
        evidence = conn.execute(
            "SELECT * FROM evidence WHERE case_id=? ORDER BY version_no",
            (case["id"],),
        ).fetchall()
        view["evidence_versions"] = [self._evidence_view(e) for e in evidence]
        recusals = conn.execute(
            "SELECT * FROM recusals WHERE case_id=? ORDER BY id", (case["id"],)
        ).fetchall()
        view["recusals"] = [self._recusal_view(conn, r) for r in recusals]
        reviewers = conn.execute(
            "SELECT u.id, u.display_name FROM reviewers r JOIN users u"
            " ON u.id=r.expert_id WHERE r.case_id=? ORDER BY r.id",
            (case["id"],),
        ).fetchall()
        view["reviewers"] = [dict(r) for r in reviewers]
        stays = conn.execute(
            "SELECT * FROM stays WHERE case_id=? ORDER BY id", (case["id"],)
        ).fetchall()
        view["stays"] = [self._stay_view(conn, s) for s in stays]
        decision = conn.execute(
            "SELECT * FROM decisions WHERE case_id=?", (case["id"],)
        ).fetchone()
        view["decision"] = self._decision_view(conn, decision) if decision else None
        return view
