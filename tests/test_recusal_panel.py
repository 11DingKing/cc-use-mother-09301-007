"""利益回避、原评审人退出、复核组与法定签署人数。"""
from __future__ import annotations

import unittest

from backend_helper import BackendTest
from appeal_review.errors import ConflictError, PermissionDenied, StateError


class RecusalPanelTest(BackendTest):
    def test_declared_recusal_blocks_assignment(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        self.svc.accept_appeal(self.secretary, "C-1")
        self.svc.declare_recusal(self.school_a, "C-1", "exp-1", "曾参与本校评价咨询")
        with self.assertRaises(ConflictError) as cm:
            self.svc.assign_panelist(self.secretary, "C-1", "exp-1")
        self.assertIn("回避", cm.exception.message)

    def test_original_reviewer_is_force_removed(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        self.svc.accept_appeal(self.secretary, "C-1")
        self.svc.assign_panelist(self.secretary, "C-1", "exp-1")
        # 事后发现 exp-1 是原评审人：必须立即退出，且无法重新加入。
        self.svc.mark_original_reviewer(self.secretary, "C-1", "exp-1")
        case = self.svc.get_case(self.secretary, "C-1")
        member = next(p for p in case["panel"] if p["expert_id"] == "exp-1")
        self.assertFalse(member["active"])
        with self.assertRaises(ConflictError):
            self.svc.assign_panelist(self.secretary, "C-1", "exp-1")

    def test_expert_self_recusal_only(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        with self.assertRaises(PermissionDenied):
            self.svc.declare_recusal(self.expert(2), "C-1", "exp-1", "代申报")

    def test_review_requires_panel_quorum(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        self.svc.accept_appeal(self.secretary, "C-1")
        self.svc.assign_panelist(self.secretary, "C-1", "exp-1")
        with self.assertRaises(ConflictError) as cm:
            self.svc.start_review(self.secretary, "C-1")
        self.assertEqual(cm.exception.code, "panel_incomplete")

    def test_decision_requires_three_valid_signatures(self) -> None:
        self.reach_review("C-1")
        self.svc.sign(self.expert(1), "C-1")
        self.svc.sign(self.expert(2), "C-1")
        with self.assertRaises(ConflictError) as cm:
            self.svc.make_decision(self.secretary, "C-1", "upheld", "理由充分，维持。")
        self.assertEqual(cm.exception.code, "quorum_unmet")
        self.svc.sign(self.expert(3), "C-1")
        case = self.svc.make_decision(self.secretary, "C-1", "upheld", "理由充分，维持。")
        self.assertEqual(case["state"], "decided")

    def test_outsider_expert_cannot_sign(self) -> None:
        self.reach_review("C-1")
        # exp-4 不在复核组：对其不可见，直接按“案件不存在”处理。
        from appeal_review.errors import NotFoundError
        with self.assertRaises(NotFoundError):
            self.svc.sign(self.expert(4), "C-1")

    def test_removed_panelist_signature_does_not_count(self) -> None:
        self.reach_review("C-1")
        self.svc.sign(self.expert(1), "C-1")
        self.svc.sign(self.expert(2), "C-1")
        self.svc.sign(self.expert(3), "C-1")
        # 签署后发现 exp-1 存在回避关系：退出后签署人数不足，不能决定。
        self.svc.declare_recusal(self.secretary, "C-1", "exp-1", "发现师生关系")
        with self.assertRaises(ConflictError) as cm:
            self.svc.make_decision(self.secretary, "C-1", "upheld", "维持。")
        self.assertEqual(cm.exception.code, "quorum_unmet")


if __name__ == "__main__":
    unittest.main()
