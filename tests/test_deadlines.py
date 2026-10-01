"""时限控制与证据版本规则。"""
from __future__ import annotations

import unittest

from backend_helper import BackendTest
from appeal_review.errors import PermissionDenied, StateError, ValidationError


class DeadlineTest(BackendTest):
    def test_acceptance_within_deadline_succeeds(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        self.clock.advance(days=4)
        case = self.svc.accept_appeal(self.secretary, "C-1")
        self.assertEqual(case["state"], "accepted")

    def test_acceptance_after_deadline_blocked_for_secretary(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        self.clock.advance(days=6)
        with self.assertRaises(StateError) as cm:
            self.svc.accept_appeal(self.secretary, "C-1")
        self.assertEqual(cm.exception.code, "accept_deadline_passed")

    def test_overdue_acceptance_requires_admin_and_reason(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        self.clock.advance(days=6)
        # 秘书不能越权逾期受理。
        with self.assertRaises(PermissionDenied):
            self.svc.accept_appeal_overdue(self.secretary, "C-1", "系统故障")
        with self.assertRaises(ValidationError):
            self.svc.accept_appeal_overdue(self.admin, "C-1", "  ")
        case = self.svc.accept_appeal_overdue(self.admin, "C-1", "机构系统故障导致延迟")
        self.assertEqual(case["state"], "accepted")

    def test_on_time_supplement_becomes_current(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        self.svc.accept_appeal(self.secretary, "C-1")
        self.svc.open_supplement_round(self.secretary, "C-1", "需补资源测算表", 10)
        self.clock.advance(days=5)
        result = self.svc.submit_documents(
            self.school_a, "C-1",
            {"documents": [self.doc("测算表.pdf", "h-v2")]}, idempotency_key="k1")
        self.assertFalse(result["submission"]["late"])
        self.assertTrue(result["submission"]["accepted_as_current"])
        current = [d for d in result["documents"] if d["current"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["content_hash"], "h-v2")

    def test_late_supplement_never_replaces_current(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        self.svc.accept_appeal(self.secretary, "C-1")
        self.svc.open_supplement_round(self.secretary, "C-1", "需补材料", 10)
        self.clock.advance(days=11)
        result = self.svc.submit_documents(
            self.school_a, "C-1",
            {"documents": [self.doc("迟交.pdf", "h-late")]}, idempotency_key="k-late")
        self.assertTrue(result["submission"]["late"])
        self.assertFalse(result["submission"]["accepted_as_current"])
        late_docs = [d for d in result["documents"] if d["late"]]
        self.assertEqual(len(late_docs), 1)
        self.assertEqual(late_docs[0]["version"], 2)
        current = [d for d in result["documents"] if d["current"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["content_hash"], "hash-1")  # 首版仍是当前版本

    def test_late_material_after_overdue_round_still_append_only(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        self.svc.accept_appeal(self.secretary, "C-1")
        self.svc.open_supplement_round(self.secretary, "C-1", "需补材料", 10)
        self.clock.advance(days=11)
        r1 = self.svc.submit_documents(
            self.school_a, "C-1", {"documents": [self.doc("a.pdf", "h-a")]},
            idempotency_key="ka")
        self.clock.advance(days=2)
        r2 = self.svc.submit_documents(
            self.school_a, "C-1", {"documents": [self.doc("b.pdf", "h-b")]},
            idempotency_key="kb")
        self.assertTrue(r1["submission"]["late"])
        self.assertTrue(r2["submission"]["late"])
        self.assertEqual(r2["submission"]["version"], 3)
        self.assertTrue(all(not d["current"] for d in r2["documents"] if d["late"]))

    def test_completed_round_blocks_second_submission(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        self.svc.accept_appeal(self.secretary, "C-1")
        self.svc.open_supplement_round(self.secretary, "C-1", "需补材料", 10)
        self.clock.advance(days=2)
        self.svc.submit_documents(
            self.school_a, "C-1", {"documents": [self.doc(h="h2")]}, idempotency_key="k1")
        with self.assertRaises(StateError):
            self.svc.submit_documents(
                self.school_a, "C-1", {"documents": [self.doc(h="h3")]}, idempotency_key="k2")

    def test_supplement_round_days_bounds(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        self.svc.accept_appeal(self.secretary, "C-1")
        with self.assertRaises(ValidationError):
            self.svc.open_supplement_round(self.secretary, "C-1", "x", 0)
        with self.assertRaises(ValidationError):
            self.svc.open_supplement_round(self.secretary, "C-1", "x", 61)


if __name__ == "__main__":
    unittest.main()
