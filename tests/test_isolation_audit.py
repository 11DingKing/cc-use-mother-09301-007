"""行级隔离与审计完整性。"""
from __future__ import annotations

import sqlite3
import unittest

from backend_helper import BackendTest
from appeal_review.errors import NotFoundError, PermissionDenied


class IsolationTest(BackendTest):
    def test_school_cannot_read_other_school_case(self) -> None:
        self.submit_simple(self.school_a, "CA")
        # 乙校查询甲校案件：直接 404，不暴露存在性。
        with self.assertRaises(NotFoundError):
            self.svc.get_case(self.school_b, "CA")

    def test_school_list_only_own_cases(self) -> None:
        self.submit_simple(self.school_a, "CA")
        self.submit_simple(self.school_b, "CB")
        ids_a = [c["id"] for c in self.svc.list_cases(self.school_a)]
        ids_b = [c["id"] for c in self.svc.list_cases(self.school_b)]
        self.assertEqual(ids_a, ["CA"])
        self.assertEqual(ids_b, ["CB"])

    def test_school_cannot_submit_to_other_school(self) -> None:
        # case_id 冲突检查在插入前，但归属以令牌账户为准；直接伪造也无法写入他校。
        self.submit_simple(self.school_a, "CA")
        with self.assertRaises(NotFoundError):
            self.svc.submit_documents(
                self.school_b, "CA", {"documents": [self.doc()]}, idempotency_key="x")

    def test_expert_sees_only_assigned_cases(self) -> None:
        self.submit_simple(self.school_a, "CA")
        self.svc.accept_appeal(self.secretary, "CA")
        self.svc.assign_panelist(self.secretary, "CA", "exp-1")
        self.assertEqual([c["id"] for c in self.svc.list_cases(self.expert(1))], ["CA"])
        self.assertEqual(self.svc.list_cases(self.expert(2)), [])
        with self.assertRaises(NotFoundError):
            self.svc.get_case(self.expert(2), "CA")

    def test_secretary_and_admin_see_all(self) -> None:
        self.submit_simple(self.school_a, "CA")
        self.submit_simple(self.school_b, "CB")
        self.assertEqual(len(self.svc.list_cases(self.secretary)), 2)
        self.assertEqual(len(self.svc.list_cases(self.admin)), 2)

    def test_institution_cannot_read_audit(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.svc.list_audit(self.school_a)


class AuditTest(BackendTest):
    def test_full_flow_is_audited(self) -> None:
        self.reach_review("C-1")
        for n in (1, 2, 3):
            self.svc.sign(self.expert(n), "C-1")
        self.svc.make_decision(self.secretary, "C-1", "upheld", "维持。")
        entries = self.svc.list_audit(self.admin, "C-1")
        actions = {e["action"] for e in entries}
        for expected in ("appeal.submit", "appeal.accept", "panel.assign",
                         "review.start", "decision.sign", "decision.make"):
            self.assertIn(expected, actions)

    def test_audit_log_is_append_only(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.read(lambda c: c.execute("DELETE FROM audit_log"))
        # 只读连接同样被触发器拦截。
        def attempt_delete(conn):
            conn.execute("DELETE FROM audit_log")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.read(attempt_delete)

    def test_audit_does_not_leak_document_content(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        entries = self.svc.list_audit(self.admin, "C-1")
        blob = str(entries)
        # 正文从不入库；审计中只出现数量与哈希引用，不出现材料正文。
        self.assertNotIn("材料正文", blob)

    def test_overdue_acceptance_is_audited_with_reason(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        self.clock.advance(days=9)
        self.svc.accept_appeal_overdue(self.admin, "C-1", "系统故障留痕")
        entries = self.svc.list_audit(self.admin, "C-1")
        entry = next(e for e in entries if e["action"] == "appeal.accept_overdue")
        self.assertEqual(entry["detail"]["reason"], "系统故障留痕")

    def test_documents_table_stores_only_metadata(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        row = self.store.read(lambda c: c.execute(
            "SELECT content_hash FROM documents WHERE case_id='C-1'").fetchone())
        self.assertEqual(row["content_hash"], "hash-1")
        columns = {d[1] for d in self.store.read(
            lambda c: c.execute("PRAGMA table_info(documents)").fetchall())}
        # 表中不存在材料正文列，正文从不入库。
        self.assertNotIn("content", columns)
        self.assertNotIn("body", columns)


if __name__ == "__main__":
    unittest.main()
