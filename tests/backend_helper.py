"""测试公共夹具：可控时钟、内存库与角色账户工厂。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from appeal_review.service import AuthContext, Service  # noqa: E402
from appeal_review.store import Clock, Store  # noqa: E402


class FixedClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self.current = start or datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)

    def now_iso(self) -> str:
        return self.current.strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, **kwargs) -> None:
        self.current += timedelta(**kwargs)


class BackendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = FixedClock()
        self.store = Store(":memory:", clock=self.clock)
        self.svc = Service(self.store)
        self._make_accounts()

    def _make_accounts(self) -> None:
        """直接落库造账户，绕过管理员接口。"""
        def tx(conn, now):
            rows = [
                ("admin-1", "admin", "管理员甲", None, "tok-admin"),
                ("sec-1", "secretary", "秘书甲", None, "tok-sec"),
                ("school-a-user", "institution", "甲校经办人", "SCHOOL-A", "tok-a"),
                ("school-b-user", "institution", "乙校经办人", "SCHOOL-B", "tok-b"),
                ("exp-1", "expert", "专家一", None, "tok-exp1"),
                ("exp-2", "expert", "专家二", None, "tok-exp2"),
                ("exp-3", "expert", "专家三", None, "tok-exp3"),
                ("exp-4", "expert", "专家四", None, "tok-exp4"),
            ]
            for uid, role, name, inst, token in rows:
                conn.execute(
                    "INSERT INTO users (id, role, name, institution_id, token) "
                    "VALUES (?,?,?,?,?)", (uid, role, name, inst, token))
        self.store.write(tx, operation="test.seed", idempotency_key=None, user_id="system")

    def auth(self, token: str) -> AuthContext:
        return self.svc.authenticate(token)

    @property
    def admin(self) -> AuthContext:
        return self.auth("tok-admin")

    @property
    def secretary(self) -> AuthContext:
        return self.auth("tok-sec")

    @property
    def school_a(self) -> AuthContext:
        return self.auth("tok-a")

    @property
    def school_b(self) -> AuthContext:
        return self.auth("tok-b")

    def expert(self, n: int) -> AuthContext:
        return self.auth(f"tok-exp{n}")

    def doc(self, name: str = "材料.pdf", h: str = "hash-1", size: int = 10) -> dict:
        return {"filename": name, "content_hash": h, "size_bytes": size}

    def submit_simple(self, auth: AuthContext, case_id: str = "C-1",
                      title: str = "分类评价申诉", key: str | None = None) -> dict:
        return self.svc.submit_appeal(
            auth, {"case_id": case_id, "title": title, "documents": [self.doc()]},
            idempotency_key=key)

    def full_panel(self, case_id: str = "C-1", experts=(1, 2, 3)) -> None:
        for n in experts:
            self.svc.assign_panelist(self.secretary, case_id, f"exp-{n}")

    def reach_review(self, case_id: str = "C-1") -> None:
        self.submit_simple(self.school_a, case_id)
        self.svc.accept_appeal(self.secretary, case_id)
        self.full_panel(case_id)
        self.svc.start_review(self.secretary, case_id)
