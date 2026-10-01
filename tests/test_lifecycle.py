"""案件合并、撤回核准、重开与决定版本差异。"""
from __future__ import annotations

import unittest

from backend_helper import BackendTest
from appeal_review.errors import ConflictError, PermissionDenied, StateError, ValidationError


class LifecycleTest(BackendTest):
    def _decide(self, case_id: str, outcome: str, rationale: str) -> dict:
        for n in (1, 2, 3):
            self.svc.sign(self.expert(n), case_id)
        return self.svc.make_decision(self.secretary, case_id, outcome, rationale)

    def test_withdrawal_needs_both_party_and_secretary(self) -> None:
        self.reach_review("C-1")
        # 院校单方撤回不生效：只是申请。
        req = self.svc.request_withdrawal(self.school_a, "C-1", "拟自行整改")
        self.assertEqual(req["status"], "pending")
        case = self.svc.get_case(self.school_a, "C-1")
        self.assertNotEqual(case["state"], "withdrawn")
        # 秘书不能代替院校发起。
        with self.assertRaises(PermissionDenied):
            self.svc.request_withdrawal(self.secretary, "C-1", "x")
        result = self.svc.decide_withdrawal(self.secretary, req["id"], True)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(self.svc.get_case(self.school_a, "C-1")["state"], "withdrawn")

    def test_institution_cannot_approve_own_withdrawal(self) -> None:
        self.reach_review("C-1")
        req = self.svc.request_withdrawal(self.school_a, "C-1", "理由")
        with self.assertRaises(PermissionDenied):
            self.svc.decide_withdrawal(self.school_a, req["id"], True)

    def test_withdrawal_rejected_keeps_case_open(self) -> None:
        self.reach_review("C-1")
        req = self.svc.request_withdrawal(self.school_a, "C-1", "理由")
        self.svc.decide_withdrawal(self.secretary, req["id"], False)
        self.assertEqual(self.svc.get_case(self.school_a, "C-1")["state"], "reviewing")

    def test_merge_requires_same_institution(self) -> None:
        self.submit_simple(self.school_a, "CA")
        self.submit_simple(self.school_b, "CB", title="乙校申诉")
        self.svc.accept_appeal(self.secretary, "CA")
        self.svc.accept_appeal(self.secretary, "CB")
        with self.assertRaises(ValidationError):
            self.svc.merge_cases(self.secretary, "CB", "CA")

    def test_merge_same_institution_closes_child(self) -> None:
        self.submit_simple(self.school_a, "CA-1")
        self.submit_simple(self.school_a, "CA-2")
        result = self.svc.merge_cases(self.secretary, "CA-2", "CA-1")
        self.assertEqual(result["state"], "merged")
        child = self.svc.get_case(self.school_a, "CA-2")
        self.assertTrue(child["closed"])
        self.assertEqual(child["merged_into"], "CA-1")

    def test_institution_cannot_merge(self) -> None:
        self.submit_simple(self.school_a, "CA-1")
        self.submit_simple(self.school_a, "CA-2")
        with self.assertRaises(PermissionDenied):
            self.svc.merge_cases(self.school_a, "CA-2", "CA-1")

    def test_active_stay_blocks_merge(self) -> None:
        self.submit_simple(self.school_a, "CA-1")
        self.submit_simple(self.school_a, "CA-2")
        self.svc.grant_stay(self.secretary, "CA-2", "防止评价结果执行")
        with self.assertRaises(ConflictError):
            self.svc.merge_cases(self.secretary, "CA-2", "CA-1")
        # 解除暂缓后可以合并。
        stay_id = self.svc.get_case(self.secretary, "CA-2")["stays"][0]["id"]
        self.svc.lift_stay(self.secretary, "CA-2", stay_id)
        self.svc.merge_cases(self.secretary, "CA-2", "CA-1")

    def test_stay_permissions(self) -> None:
        self.submit_simple(self.school_a, "C-1")
        with self.assertRaises(PermissionDenied):
            self.svc.grant_stay(self.school_a, "C-1", "请求暂缓")

    def test_reopen_is_admin_only_and_preserves_decision_versions(self) -> None:
        self.reach_review("C-1")
        self._decide("C-1", "upheld", "第一行理由\n维持原分类。")
        with self.assertRaises(PermissionDenied):
            self.svc.reopen_case(self.secretary, "C-1", "新证据")
        reopened = self.svc.reopen_case(self.admin, "C-1", "出现未质证的关键材料")
        self.assertEqual(reopened["state"], "reopened")
        # 旧签署全部失效，重开后须重新满足法定人数。
        self.assertEqual(reopened["signature_count"], 0)
        self.assertEqual(len(reopened["decisions"]), 1)

    def test_reopened_case_new_decision_shows_diff(self) -> None:
        self.reach_review("C-1")
        self._decide("C-1", "upheld", "维持原评价分类。")
        self.svc.reopen_case(self.admin, "C-1", "新证据")
        # 重开后原复核组仍在任，需要重新签署。
        for n in (1, 2, 3):
            self.svc.sign(self.expert(n), "C-1")
        case = self.svc.make_decision(
            self.secretary, "C-1", "modified", "调整资源安排口径。\n变更分类档次。")
        self.assertEqual(len(case["decisions"]), 2)
        latest = case["decisions"][1]
        diff = latest["diff_from_previous"]
        self.assertIsNotNone(diff)
        self.assertTrue(diff["outcome_changed"])
        self.assertEqual(diff["previous_outcome"], "upheld")
        self.assertEqual(len(diff["rationale_lines_added"]), 2)
        self.assertEqual(len(diff["rationale_lines_removed"]), 1)

    def test_cannot_decide_without_rereaching_quorum_after_reopen(self) -> None:
        self.reach_review("C-1")
        self._decide("C-1", "upheld", "维持。")
        self.svc.reopen_case(self.admin, "C-1", "理由")
        with self.assertRaises(ConflictError) as cm:
            self.svc.make_decision(self.secretary, "C-1", "revoked", "撤销。")
        self.assertEqual(cm.exception.code, "quorum_unmet")

    def test_decided_case_is_closed_and_cannot_withdraw(self) -> None:
        self.reach_review("C-1")
        self._decide("C-1", "upheld", "维持。")
        with self.assertRaises(StateError):
            self.svc.request_withdrawal(self.school_a, "C-1", "想撤")


if __name__ == "__main__":
    unittest.main()
