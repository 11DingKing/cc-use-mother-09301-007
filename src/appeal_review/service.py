"""领域服务层：所有业务规则集中于此，HTTP 层只做参数编解码。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .errors import ConflictError, NotFoundError, PermissionDenied, StateError, ValidationError
from .store import Store

ROLES = ("institution", "secretary", "expert", "admin")
OPEN_STATES = ("submitted", "accepted", "supplementing", "reviewing", "reopened")
DECIDABLE_FROM = ("reviewing", "reopened")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _add_days(ts: str, days: int) -> str:
    return (_parse(ts) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    role: str
    name: str
    institution_id: str | None

    @property
    def is_secretary(self) -> bool:
        return self.role == "secretary"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Service:
    def __init__(self, store: Store, settings: Settings | None = None) -> None:
        self.store = store
        self.settings = settings or store.settings

    # -- 用户与认证 ----------------------------------------------------------

    def admin_create_user(self, auth: AuthContext, user_id: str, role: str, name: str,
                          institution_id: str | None, token: str | None, *,
                          idempotency_key: str | None = None) -> dict:
        if not auth.is_admin:
            raise PermissionDenied("仅管理员可创建用户")

        def tx(conn, now):
            return self.create_user(conn, user_id, role, name, institution_id, token)
        return self.write_as(auth, "user.create", tx, idempotency_key=idempotency_key)

    def create_user(self, conn: sqlite3.Connection, user_id: str, role: str, name: str,
                    institution_id: str | None, token: str | None) -> dict:
        if role not in ROLES:
            raise ValidationError("角色非法")
        if role == "institution" and not institution_id:
            raise ValidationError("院校账户必须归属院校")
        if role != "institution" and institution_id:
            raise ValidationError("非院校账户不得归属院校")
        if not user_id or not name:
            raise ValidationError("用户编号与姓名必填")
        conn.execute(
            "INSERT INTO users (id, role, name, institution_id, token) VALUES (?,?,?,?,?)",
            (user_id, role, name, institution_id, token),
        )
        Store.audit(conn, user_id, "user.create", "user", user_id,
                    {"role": role, "institution_id": institution_id})
        return {"id": user_id, "role": role, "name": name, "institution_id": institution_id}

    def authenticate(self, token: str | None) -> AuthContext:
        if not token:
            raise PermissionDenied("缺少认证令牌", code="unauthorized")
        row = self.store.read(
            lambda c: c.execute(
                "SELECT id, role, name, institution_id FROM users WHERE token=?", (token,)
            ).fetchone()
        )
        if row is None:
            raise PermissionDenied("令牌无效", code="unauthorized")
        return AuthContext(row["id"], row["role"], row["name"], row["institution_id"])

    def write_as(self, auth: AuthContext, operation: str, fn, *,
                 idempotency_key: str | None = None):
        result = self.store.write(fn, operation=operation,
                                  idempotency_key=idempotency_key, user_id=auth.user_id)
        if isinstance(result, dict) and "__idempotent_replay__" in result:
            return result["__idempotent_replay__"]
        return result

    # -- 读取与行级可见性 ----------------------------------------------------

    def _load_case(self, conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise NotFoundError("案件不存在")
        return row

    def _require_case_access(self, conn: sqlite3.Connection, auth: AuthContext,
                             case: sqlite3.Row) -> None:
        """院校只见本校案件；专家只在复核组在任期间可见；秘书/管理员不受限。

        管理员可见的是过程元数据：证据表只存哈希与文件名，不含材料正文。
        """
        if auth.role in ("secretary", "admin"):
            return
        if auth.role == "institution":
            if auth.institution_id != case["institution_id"]:
                raise NotFoundError("案件不存在")  # 不暴露他校案件的存在性
            return
        if auth.role == "expert":
            row = conn.execute(
                "SELECT 1 FROM panel_assignments WHERE case_id=? AND expert_id=? AND removed=0",
                (case["id"], auth.user_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("案件不存在")

    def get_case(self, auth: AuthContext, case_id: str) -> dict:
        def tx(conn):
            case = self._load_case(conn, case_id)
            self._require_case_access(conn, auth, case)
            return self._serialize_case(conn, case, auth)
        return self.store.read(tx)

    def list_cases(self, auth: AuthContext) -> list[dict]:
        def tx(conn):
            if auth.role == "institution":
                rows = conn.execute(
                    "SELECT * FROM cases WHERE institution_id=? ORDER BY created_at",
                    (auth.institution_id,),
                ).fetchall()
            elif auth.role == "expert":
                rows = conn.execute(
                    "SELECT c.* FROM cases c JOIN panel_assignments p ON p.case_id=c.id "
                    "WHERE p.expert_id=? AND p.removed=0 ORDER BY c.created_at",
                    (auth.user_id,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM cases ORDER BY created_at").fetchall()
            return [self._serialize_case(conn, r, auth, summary=True) for r in rows]
        return self.store.read(tx)

    def _serialize_case(self, conn, case: sqlite3.Row, auth: AuthContext, *,
                        summary: bool = False) -> dict:
        data = {
            "id": case["id"],
            "institution_id": case["institution_id"],
            "title": case["title"],
            "category": case["category"],
            "state": case["state"],
            "closed": bool(case["closed"]),
            "created_at": case["created_at"],
            "accepted_at": case["accepted_at"],
            "accept_due": case["accept_due"],
            "decided_at": case["decided_at"],
            "merged_into": case["merged_into"],
        }
        if summary:
            return data
        data["rounds"] = [
            {"id": r["id"], "reason": r["reason"], "due_at": r["due_at"],
             "opened_at": r["opened_at"], "submitted_at": r["submitted_at"],
             "late": bool(r["late"])}
            for r in conn.execute(
                "SELECT * FROM supplement_rounds WHERE case_id=? ORDER BY id", (case["id"],))
        ]
        data["documents"] = [
            {"version": d["version"], "filename": d["filename"],
             "content_hash": d["content_hash"], "size_bytes": d["size_bytes"],
             "submitted_at": d["submitted_at"], "late": bool(d["late"]),
             "current": bool(d["current"])}
            for d in conn.execute(
                "SELECT * FROM documents WHERE case_id=? ORDER BY version, filename",
                (case["id"],))
        ]
        data["recusals"] = [
            {"expert_id": r["expert_id"], "reason": r["reason"], "declared_by": r["declared_by"]}
            for r in conn.execute("SELECT * FROM recusals WHERE case_id=?", (case["id"],))
        ]
        data["panel"] = [
            {"expert_id": p["expert_id"], "active": not p["removed"],
             "assigned_at": p["assigned_at"], "removed_at": p["removed_at"]}
            for p in conn.execute(
                "SELECT * FROM panel_assignments WHERE case_id=? ORDER BY id", (case["id"],))
        ]
        sigs = conn.execute(
            "SELECT expert_id, signed_at, revoked FROM signatures WHERE case_id=? AND revoked=0",
            (case["id"],),
        ).fetchall()
        data["signatures"] = [{"expert_id": s["expert_id"], "signed_at": s["signed_at"]} for s in sigs]
        data["signature_count"] = len(sigs)
        data["required_signatures"] = self.settings.required_signatures
        data["stays"] = [
            {"id": s["id"], "reason": s["reason"], "active": bool(s["active"]),
             "granted_by": s["granted_by"], "granted_at": s["granted_at"],
             "lifted_by": s["lifted_by"], "lifted_at": s["lifted_at"]}
            for s in conn.execute("SELECT * FROM stays WHERE case_id=? ORDER BY id", (case["id"],))
        ]
        decisions = conn.execute(
            "SELECT * FROM decisions WHERE case_id=? ORDER BY version", (case["id"],)
        ).fetchall()
        data["decisions"] = [self._serialize_decision(conn, d, decisions[i - 1] if i else None)
                             for i, d in enumerate(decisions)]
        return data

    def _serialize_decision(self, conn, d: sqlite3.Row, prev: sqlite3.Row | None) -> dict:
        result = {
            "version": d["version"],
            "outcome": d["outcome"],
            "rationale": d["rationale"],
            "signed_by": json.loads(d["signed_by"]),
            "created_at": d["created_at"],
            "diff_from_previous": None,
        }
        if prev is not None:
            result["diff_from_previous"] = self._diff_decisions(prev, d)
        return result

    @staticmethod
    def _diff_decisions(old: sqlite3.Row, new: sqlite3.Row) -> dict:
        """逐字段决定差异：结论是否变化、理由中增删的行。"""
        import difflib
        delta = list(difflib.ndiff(old["rationale"].splitlines(), new["rationale"].splitlines()))
        added = [line[2:] for line in delta if line.startswith("+ ")]
        removed = [line[2:] for line in delta if line.startswith("- ")]
        return {
            "outcome_changed": old["outcome"] != new["outcome"],
            "previous_outcome": old["outcome"],
            "rationale_lines_added": added,
            "rationale_lines_removed": removed,
        }

    # -- 申诉提交 ------------------------------------------------------------

    def submit_appeal(self, auth: AuthContext, data: dict, *, idempotency_key: str | None) -> dict:
        if auth.role != "institution":
            raise PermissionDenied("仅申诉院校可提交申诉")
        case_id = str(data.get("case_id") or "").strip()
        title = str(data.get("title") or "").strip()
        if not case_id or not title:
            raise ValidationError("案件编号与标题必填")
        docs = self._validate_documents(data.get("documents"))

        def tx(conn, now):
            if conn.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone():
                raise ConflictError("案件编号已存在")
            conn.execute(
                "INSERT INTO cases (id, institution_id, title, category, state, accept_due) "
                "VALUES (?,?,?,?, 'submitted', ?)",
                (case_id, auth.institution_id, title, data.get("category"),
                 _add_days(now, self.settings.accept_deadline_days)),
            )
            for i, doc in enumerate(docs):
                conn.execute(
                    "INSERT INTO documents (case_id, version, filename, content_hash, size_bytes,"
                    " submitted_by, submitted_at, current) VALUES (?,1,?,?,?,?,?,1)",
                    (case_id, doc["filename"], doc["content_hash"], doc["size_bytes"],
                     auth.user_id, now),
                )
            Store.audit(conn, auth.user_id, "appeal.submit", "case", case_id,
                        {"title": title, "documents": len(docs)})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "appeal.submit", tx, idempotency_key=idempotency_key)

    @staticmethod
    def _validate_documents(value: Any) -> list[dict]:
        if not isinstance(value, list) or not value:
            raise ValidationError("至少提交一份证据（哈希+文件名+字节数）")
        docs = []
        for item in value:
            if not isinstance(item, dict):
                raise ValidationError("证据条目格式错误")
            filename = str(item.get("filename") or "").strip()
            content_hash = str(item.get("content_hash") or "").strip()
            size = item.get("size_bytes")
            if not filename or not content_hash:
                raise ValidationError("证据缺少文件名或内容哈希")
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValidationError("证据字节数必须是非负整数")
            docs.append({"filename": filename, "content_hash": content_hash, "size_bytes": size})
        return docs

    # -- 受理 ----------------------------------------------------------------

    def accept_appeal(self, auth: AuthContext, case_id: str) -> dict:
        if not auth.is_secretary:
            raise PermissionDenied("仅复核秘书可受理")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            if case["state"] != "submitted":
                raise StateError("仅“提交”状态的案件可受理")
            if now > case["accept_due"]:
                raise StateError(
                    "受理期限已过，须由管理员按例外程序处理",
                    code="accept_deadline_passed",
                )
            conn.execute(
                "UPDATE cases SET state='accepted', accepted_at=? WHERE id=?", (now, case_id))
            Store.audit(conn, auth.user_id, "appeal.accept", "case", case_id, {"at": now})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "appeal.accept", tx)

    def accept_appeal_overdue(self, auth: AuthContext, case_id: str, reason: str) -> dict:
        """逾期受理属例外：仅管理员、必须书面理由，全部入审计。"""
        if not auth.is_admin:
            raise PermissionDenied("逾期受理仅管理员可批准")
        if not reason.strip():
            raise ValidationError("逾期受理必须说明理由")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            if case["state"] != "submitted":
                raise StateError("仅“提交”状态的案件可受理")
            conn.execute(
                "UPDATE cases SET state='accepted', accepted_at=? WHERE id=?", (now, case_id))
            Store.audit(conn, auth.user_id, "appeal.accept_overdue", "case", case_id,
                        {"at": now, "reason": reason})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "appeal.accept_overdue", tx)

    # -- 补证轮次与证据版本 --------------------------------------------------

    def open_supplement_round(self, auth: AuthContext, case_id: str, reason: str,
                              days: int | None) -> dict:
        if not auth.is_secretary:
            raise PermissionDenied("仅复核秘书可发出补证通知")
        if not reason.strip():
            raise ValidationError("补证通知必须说明事项")
        days = days if days is not None else self.settings.supplement_deadline_days
        if not (self.settings.supplement_min_days <= days <= self.settings.supplement_max_days):
            raise ValidationError(
                f"补证期限须在 {self.settings.supplement_min_days}-"
                f"{self.settings.supplement_max_days} 天之间")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            if case["state"] not in ("accepted", "supplementing"):
                raise StateError("受理后、复核开始前才能要求补证")
            due = _add_days(now, days)
            cur = conn.execute(
                "SELECT id FROM supplement_rounds WHERE case_id=? AND submitted_at IS NULL",
                (case_id,)).fetchone()
            if cur is not None:
                raise ConflictError("已有未完成的补证轮次")
            conn.execute(
                "INSERT INTO supplement_rounds (case_id, reason, due_at) VALUES (?,?,?)",
                (case_id, reason, due))
            conn.execute("UPDATE cases SET state='supplementing' WHERE id=?", (case_id,))
            Store.audit(conn, auth.user_id, "supplement.round_open", "case", case_id,
                        {"reason": reason, "due_at": due})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "supplement.round_open", tx)

    def submit_documents(self, auth: AuthContext, case_id: str, data: dict, *,
                         idempotency_key: str | None) -> dict:
        """院校提交证据。

        逾期判定以系统登记的补证期限为准（不再依赖邮件时间）：逾期材料一律
        标记 ``late=1``，只作为新版本留痕，永不替换当前有效版本。
        """
        if auth.role != "institution":
            raise PermissionDenied("仅申诉院校可提交证据")
        docs = self._validate_documents(data.get("documents"))

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            if auth.institution_id != case["institution_id"]:
                raise NotFoundError("案件不存在")
            if case["state"] not in ("submitted", "accepted", "supplementing"):
                raise StateError("当前状态不再接收证据")
            round_row = None
            if case["state"] == "supplementing":
                round_row = conn.execute(
                    "SELECT * FROM supplement_rounds WHERE case_id=? ORDER BY id DESC LIMIT 1",
                    (case_id,)).fetchone()
                if round_row is None:
                    raise StateError("没有补证轮次")
                if round_row["submitted_at"] is not None and not round_row["late"]:
                    raise StateError("本轮补证已按期完成；如需再补须由秘书另开轮次")
            if round_row is None:
                late = False
            elif round_row["late"]:
                late = True  # 轮次已逾期关闭，后续材料一律逾期
            elif round_row["submitted_at"] is not None:
                late = False  # 已被上方按期完成的检查拦截
            else:
                late = now > round_row["due_at"]
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS v FROM documents WHERE case_id=?",
                (case_id,)).fetchone()["v"]
            for doc in docs:
                conn.execute(
                    "INSERT INTO documents (case_id, round_id, version, filename, content_hash,"
                    " size_bytes, submitted_by, submitted_at, late, current)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (case_id, round_row["id"] if round_row else None, version,
                     doc["filename"], doc["content_hash"], doc["size_bytes"],
                     auth.user_id, now, int(late), int(not late)))
            if not late:
                conn.execute(
                    "UPDATE documents SET current=0 WHERE case_id=? AND version<>?",
                    (case_id, version))
                conn.execute(
                    "UPDATE supplement_rounds SET submitted_at=?, late=0 WHERE id=?",
                    (now, round_row["id"]))
            else:
                # 逾期材料只留痕为新版本；轮次首次逾期时即关闭，不再改变其关闭时间。
                if round_row["submitted_at"] is None:
                    conn.execute(
                        "UPDATE supplement_rounds SET submitted_at=?, late=1 WHERE id=?",
                        (now, round_row["id"]))
            Store.audit(conn, auth.user_id, "document.submit", "case", case_id,
                        {"version": version, "late": late, "count": len(docs)})
            result = self._serialize_case(conn, self._load_case(conn, case_id), auth)
            result["submission"] = {"version": version, "late": late,
                                    "accepted_as_current": not late}
            return result
        return self.write_as(auth, "document.submit", tx, idempotency_key=idempotency_key)

    # -- 回避与复核组 --------------------------------------------------------

    def declare_recusal(self, auth: AuthContext, case_id: str, expert_id: str, reason: str) -> dict:
        if auth.role not in ("secretary", "institution", "expert"):
            raise PermissionDenied("无权申报回避")
        if not reason.strip():
            raise ValidationError("回避必须说明理由")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            if auth.role == "institution" and auth.institution_id != case["institution_id"]:
                raise NotFoundError("案件不存在")
            if auth.role == "expert" and auth.user_id != expert_id:
                raise PermissionDenied("专家只能申报本人回避")
            expert = conn.execute("SELECT role FROM users WHERE id=?", (expert_id,)).fetchone()
            if expert is None or expert["role"] != "expert":
                raise ValidationError("目标不是独立专家")
            conn.execute(
                "INSERT OR IGNORE INTO recusals (case_id, expert_id, reason, declared_by)"
                " VALUES (?,?,?,?)",
                (case_id, expert_id, reason, auth.user_id))
            # 命中回避即在任复核组中退出，且不可再被指派；其已有签署一并失效。
            conn.execute(
                "UPDATE panel_assignments SET removed=1, removed_at=? "
                "WHERE case_id=? AND expert_id=? AND removed=0",
                (now, case_id, expert_id))
            conn.execute(
                "UPDATE signatures SET revoked=1, revoked_at=? "
                "WHERE case_id=? AND expert_id=? AND revoked=0",
                (now, case_id, expert_id))
            Store.audit(conn, auth.user_id, "recusal.declare", "case", case_id,
                        {"expert_id": expert_id, "reason": reason})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "recusal.declare", tx)

    def mark_original_reviewer(self, auth: AuthContext, case_id: str, expert_id: str) -> dict:
        """登记原评审人：系统强制其退出复核，后续无法再入组。"""
        if not auth.is_secretary:
            raise PermissionDenied("仅复核秘书可登记原评审人")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            expert = conn.execute("SELECT role FROM users WHERE id=?", (expert_id,)).fetchone()
            if expert is None or expert["role"] != "expert":
                raise ValidationError("目标不是独立专家")
            conn.execute(
                "INSERT OR IGNORE INTO recusals (case_id, expert_id, reason, declared_by)"
                " VALUES (?,?, '原评审人，依法回避', ?)",
                (case_id, expert_id, auth.user_id))
            conn.execute(
                "UPDATE panel_assignments SET removed=1, removed_at=? "
                "WHERE case_id=? AND expert_id=? AND removed=0",
                (now, case_id, expert_id))
            conn.execute(
                "UPDATE signatures SET revoked=1, revoked_at=? "
                "WHERE case_id=? AND expert_id=? AND revoked=0",
                (now, case_id, expert_id))
            Store.audit(conn, auth.user_id, "recusal.original_reviewer", "case", case_id,
                        {"expert_id": expert_id})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "recusal.original_reviewer", tx)

    def assign_panelist(self, auth: AuthContext, case_id: str, expert_id: str) -> dict:
        if not auth.is_secretary:
            raise PermissionDenied("仅复核秘书可组织复核组")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            if case["state"] not in ("accepted", "supplementing", "reviewing", "reopened"):
                raise StateError("受理后才能组建复核组")
            expert = conn.execute("SELECT role FROM users WHERE id=?", (expert_id,)).fetchone()
            if expert is None or expert["role"] != "expert":
                raise ValidationError("目标不是独立专家")
            if conn.execute(
                "SELECT 1 FROM recusals WHERE case_id=? AND expert_id=?",
                (case_id, expert_id)).fetchone():
                raise ConflictError("该专家与本案存在回避关系（含原评审人），不得进入复核组")
            exists = conn.execute(
                "SELECT removed FROM panel_assignments WHERE case_id=? AND expert_id=?",
                (case_id, expert_id)).fetchone()
            if exists is not None:
                if not exists["removed"]:
                    raise ConflictError("专家已在复核组")
                raise ConflictError("该专家已退出本案复核，不得重新加入")
            conn.execute(
                "INSERT INTO panel_assignments (case_id, expert_id, assigned_by) VALUES (?,?,?)",
                (case_id, expert_id, auth.user_id))
            Store.audit(conn, auth.user_id, "panel.assign", "case", case_id,
                        {"expert_id": expert_id})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "panel.assign", tx)

    def _active_panel(self, conn: sqlite3.Connection, case_id: str) -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT p.expert_id FROM panel_assignments p WHERE p.case_id=? AND p.removed=0",
            (case_id,)).fetchall()

    def start_review(self, auth: AuthContext, case_id: str) -> dict:
        if not auth.is_secretary:
            raise PermissionDenied("仅复核秘书可启动复核")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            if case["state"] not in ("accepted", "supplementing"):
                raise StateError("受理后的案件才能进入复核")
            panel = self._active_panel(conn, case_id)
            if len(panel) < self.settings.min_panel_size:
                raise ConflictError(
                    f"复核组不足法定人数（{len(panel)}/{self.settings.min_panel_size}）",
                    code="panel_incomplete")
            blocked = conn.execute(
                "SELECT p.expert_id FROM panel_assignments p JOIN recusals r "
                "ON r.case_id=p.case_id AND r.expert_id=p.expert_id "
                "WHERE p.case_id=? AND p.removed=0", (case_id,)).fetchall()
            if blocked:
                raise ConflictError("复核组仍存在应回避成员",
                                    details={"experts": [b["expert_id"] for b in blocked]})
            conn.execute("UPDATE cases SET state='reviewing' WHERE id=?", (case_id,))
            Store.audit(conn, auth.user_id, "review.start", "case", case_id,
                        {"panel_size": len(panel)})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "review.start", tx)

    # -- 签署与决定 ----------------------------------------------------------

    def sign(self, auth: AuthContext, case_id: str) -> dict:
        if auth.role != "expert":
            raise PermissionDenied("仅独立专家可签署")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            self._require_case_access(conn, auth, case)
            if case["state"] not in ("reviewing", "reopened"):
                raise StateError("案件尚未进入复核")
            conn.execute(
                "INSERT INTO signatures (case_id, expert_id) VALUES (?,?) "
                "ON CONFLICT(case_id, expert_id) DO UPDATE SET "
                "revoked=0, revoked_at=NULL WHERE signatures.revoked=1",
                (case_id, auth.user_id))
            Store.audit(conn, auth.user_id, "decision.sign", "case", case_id, {})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "decision.sign", tx)

    OUTCOMES = ("upheld", "modified", "revoked", "remanded")

    def make_decision(self, auth: AuthContext, case_id: str, outcome: str, rationale: str) -> dict:
        if not auth.is_secretary:
            raise PermissionDenied("仅复核秘书可在法定签署人数达标后代为作出决定")
        if outcome not in self.OUTCOMES:
            raise ValidationError("决定结论非法")
        if not rationale.strip():
            raise ValidationError("决定必须载明理由")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            if case["state"] not in DECIDABLE_FROM:
                raise StateError("仅复核中的案件可作出决定")
            panel_ids = {r["expert_id"] for r in self._active_panel(conn, case_id)}
            sigs = conn.execute(
                "SELECT expert_id FROM signatures WHERE case_id=? AND revoked=0", (case_id,)
            ).fetchall()
            valid = [s for s in sigs if s["expert_id"] in panel_ids]
            if len(valid) < self.settings.required_signatures:
                raise ConflictError(
                    f"法定签署人数不足（{len(valid)}/{self.settings.required_signatures}）",
                    code="quorum_unmet")
            if len(panel_ids) < self.settings.min_panel_size:
                raise ConflictError("复核组不足法定人数", code="panel_incomplete")
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS v FROM decisions WHERE case_id=?",
                (case_id,)).fetchone()["v"]
            signers = sorted(s["expert_id"] for s in valid)
            conn.execute(
                "INSERT INTO decisions (case_id, version, outcome, rationale, signed_by, created_by)"
                " VALUES (?,?,?,?,?,?)",
                (case_id, version, outcome, rationale, json.dumps(signers), auth.user_id))
            conn.execute(
                "UPDATE cases SET state='decided', decided_at=?, closed=1 WHERE id=?",
                (now, case_id))
            Store.audit(conn, auth.user_id, "decision.make", "case", case_id,
                        {"version": version, "outcome": outcome,
                         "signature_count": len(signers)})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "decision.make", tx)

    # -- 暂缓执行 ------------------------------------------------------------

    def grant_stay(self, auth: AuthContext, case_id: str, reason: str) -> dict:
        if not auth.is_secretary:
            raise PermissionDenied("仅复核秘书可决定暂缓执行")
        if not reason.strip():
            raise ValidationError("暂缓措施必须说明理由")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            if case["state"] in ("merged", "withdrawn"):
                raise StateError("已终结案件不能再采取暂缓措施")
            if conn.execute(
                "SELECT 1 FROM stays WHERE case_id=? AND active=1", (case_id,)).fetchone():
                raise ConflictError("已存在生效的暂缓措施")
            cur = conn.execute(
                "INSERT INTO stays (case_id, reason, granted_by) VALUES (?,?,?)",
                (case_id, reason, auth.user_id))
            Store.audit(conn, auth.user_id, "stay.grant", "case", case_id,
                        {"stay_id": cur.lastrowid, "reason": reason})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "stay.grant", tx)

    def lift_stay(self, auth: AuthContext, case_id: str, stay_id: int) -> dict:
        if not auth.is_secretary:
            raise PermissionDenied("仅复核秘书可解除暂缓执行")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            stay = conn.execute("SELECT * FROM stays WHERE id=? AND case_id=?",
                                (stay_id, case_id)).fetchone()
            if stay is None:
                raise NotFoundError("暂缓措施不存在")
            if not stay["active"]:
                raise ConflictError("措施已解除")
            conn.execute(
                "UPDATE stays SET active=0, lifted_by=?, lifted_at=? WHERE id=?",
                (auth.user_id, now, stay_id))
            Store.audit(conn, auth.user_id, "stay.lift", "case", case_id, {"stay_id": stay_id})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "stay.lift", tx)

    # -- 撤回（院校申请、秘书核准）-------------------------------------------

    def request_withdrawal(self, auth: AuthContext, case_id: str, reason: str) -> dict:
        if auth.role != "institution":
            raise PermissionDenied("仅申诉院校可申请撤回")
        if not reason.strip():
            raise ValidationError("撤回必须说明理由")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            if auth.institution_id != case["institution_id"]:
                raise NotFoundError("案件不存在")
            if case["state"] in ("decided", "merged", "withdrawn"):
                raise StateError("已终结案件不能撤回")
            pending = conn.execute(
                "SELECT 1 FROM withdrawal_requests WHERE case_id=? AND status='pending'",
                (case_id,)).fetchone()
            if pending:
                raise ConflictError("撤回申请尚在核准中")
            cur = conn.execute(
                "INSERT INTO withdrawal_requests (case_id, reason, requested_by) VALUES (?,?,?)",
                (case_id, reason, auth.user_id))
            Store.audit(conn, auth.user_id, "withdrawal.request", "case", case_id,
                        {"request_id": cur.lastrowid})
            return {"id": cur.lastrowid, "case_id": case_id, "status": "pending"}
        return self.write_as(auth, "withdrawal.request", tx)

    def decide_withdrawal(self, auth: AuthContext, request_id: int, approve: bool) -> dict:
        if not auth.is_secretary:
            raise PermissionDenied("撤回须经复核秘书核准")

        def tx(conn, now):
            req = conn.execute(
                "SELECT * FROM withdrawal_requests WHERE id=?", (request_id,)).fetchone()
            if req is None:
                raise NotFoundError("撤回申请不存在")
            if req["status"] != "pending":
                raise ConflictError("申请已处理")
            case = self._load_case(conn, req["case_id"])
            if approve:
                if case["state"] in ("decided", "merged", "withdrawn"):
                    raise StateError("案件已终结，无法核准撤回")
                conn.execute(
                    "UPDATE cases SET state='withdrawn', closed=1 WHERE id=?", (case["id"],))
            conn.execute(
                "UPDATE withdrawal_requests SET status=?, decided_by=?, decided_at=? WHERE id=?",
                ("approved" if approve else "rejected", auth.user_id, now, request_id))
            Store.audit(conn, auth.user_id,
                        "withdrawal.approve" if approve else "withdrawal.reject",
                        "withdrawal_request", str(request_id), {"case_id": case["id"]})
            return {"id": request_id, "case_id": case["id"],
                    "status": "approved" if approve else "rejected"}
        return self.write_as(auth, "withdrawal.decide", tx)

    # -- 合并（秘书权限，限同院校，活跃暂缓阻止）------------------------------

    def merge_cases(self, auth: AuthContext, child_id: str, parent_id: str) -> dict:
        if not auth.is_secretary:
            raise PermissionDenied("仅复核秘书可合并案件")
        if child_id == parent_id:
            raise ValidationError("不能将案件并入自身")

        def tx(conn, now):
            child, parent = self._load_case(conn, child_id), self._load_case(conn, parent_id)
            if child["institution_id"] != parent["institution_id"]:
                raise ValidationError("不同院校的案件不得合并（防止材料交叉泄露）")
            if child["closed"] or parent["closed"]:
                raise StateError("已终结案件不能合并")
            if child["merged_into"]:
                raise StateError("案件已被并入其他案件")
            if conn.execute(
                "SELECT 1 FROM stays WHERE case_id=? AND active=1", (child_id,)).fetchone():
                raise ConflictError("被合并案件存在生效暂缓措施，须先解除")
            conn.execute(
                "UPDATE cases SET state='merged', closed=1, merged_into=? WHERE id=?",
                (parent_id, child_id))
            conn.execute(
                "INSERT INTO case_links (child_id, parent_id, kind, created_by)"
                " VALUES (?,?,'merge',?)",
                (child_id, parent_id, auth.user_id))
            Store.audit(conn, auth.user_id, "case.merge", "case", child_id,
                        {"parent_id": parent_id})
            return {"child_id": child_id, "parent_id": parent_id, "state": "merged"}
        return self.write_as(auth, "case.merge", tx)

    # -- 重开（管理员权限，决定版本全部保留以便差异比对）----------------------

    def reopen_case(self, auth: AuthContext, case_id: str, reason: str) -> dict:
        if not auth.is_admin:
            raise PermissionDenied("重开已决案件仅管理员可执行")
        if not reason.strip():
            raise ValidationError("重开必须说明理由")

        def tx(conn, now):
            case = self._load_case(conn, case_id)
            if case["state"] not in ("decided", "withdrawn"):
                raise StateError("仅已决定或已撤回的案件可重开")
            conn.execute(
                "UPDATE cases SET state='reopened', closed=0 WHERE id=?", (case_id,))
            conn.execute(
                "INSERT INTO case_links (child_id, parent_id, kind, created_by)"
                " VALUES (?,?, 'reopen', ?)",
                (case_id, case_id, auth.user_id))
            # 重开后旧签署一律失效，需重新达到法定人数。
            conn.execute(
                "UPDATE signatures SET revoked=1, revoked_at=? WHERE case_id=? AND revoked=0",
                (now, case_id))
            Store.audit(conn, auth.user_id, "case.reopen", "case", case_id, {"reason": reason})
            return self._serialize_case(conn, self._load_case(conn, case_id), auth)
        return self.write_as(auth, "reopen_case", tx)

    # -- 审计日志（管理员全过程；不含材料正文，正文从不入库）------------------

    def list_audit(self, auth: AuthContext, case_id: str | None = None,
                   limit: int = 200) -> list[dict]:
        if auth.role not in ("admin", "secretary"):
            raise PermissionDenied("审计日志仅对复核机构开放")
        limit = max(1, min(limit, 1000))

        def tx(conn):
            sql = "SELECT * FROM audit_log"
            params: list[Any] = []
            if case_id:
                sql += (" WHERE entity_id=? OR json_extract(detail_json, '$.case_id')=?")
                params += [case_id, case_id]
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            return [
                {"id": r["id"], "ts": r["ts"], "actor_id": r["actor_id"],
                 "action": r["action"], "entity": r["entity"], "entity_id": r["entity_id"],
                 "detail": json.loads(r["detail_json"])}
                for r in conn.execute(sql, params)
            ]
        return self.store.read(tx)
