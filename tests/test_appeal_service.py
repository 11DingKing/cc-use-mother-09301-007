"""领域服务测试：时限、回避、签署人数、决定差异、幂等、权限与审计隔离。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from appeal_review.errors import (  # noqa: E402
    ConflictError,
    NotFoundError,
    PermissionError,
    QuorumError,
    StateConflictError,
)
from appeal_review.service import AppealService  # noqa: E402


class ServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = AppealService(":memory:", required_signatures=3)
        self.school_a = self._user("school_a")
        self.school_b = self._user("school_b")
        self.sec = self._user("sec")
        self.admin = self._user("admin")
        self.e1 = self._user("expert1")
        self.e2 = self._user("expert2")
        self.e3 = self._user("expert3")
        self.orig = self._user("orig")

    def _user(self, username: str):
        token = self.svc.login(username)["token"]
        return self.svc.authenticate(token)

    def _new_case(self, user=None, key=None, subject="分类评价资源安排申诉"):
        _, body = self.svc.create_case(user or self.school_a,
                                       {"subject": subject}, key)
        return body

    # ------------------------------------------------------------ 幂等提交

    def test_idempotent_submit_replays_first_result(self) -> None:
        payload = {"subject": "幂等申诉"}
        _, first = self.svc.create_case(self.school_a, payload, "key-1")
        _, replay = self.svc.create_case(self.school_a, payload, "key-1")
        self.assertEqual(first["id"], replay["id"])
        self.assertEqual(first["case_no"], replay["case_no"])

    def test_same_key_different_payload_rejected(self) -> None:
        self.svc.create_case(self.school_a, {"subject": "甲"}, "key-2")
        with self.assertRaises(ConflictError):
            self.svc.create_case(self.school_a, {"subject": "乙"}, "key-2")

    def test_without_key_each_submit_creates_case(self) -> None:
        first = self._new_case()
        second = self._new_case()
        self.assertNotEqual(first["id"], second["id"])

    # ------------------------------------------------------------ 时限控制

    def _supplement_window(self, case_ref: str, days: int = 10):
        self.svc.accept_case(self.sec, case_ref, None)
        self.svc.request_supplement(self.sec, case_ref, {"days": days}, None)

    def _expire_deadline(self, case_id: int) -> None:
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(
            timespec="seconds")
        self.svc.conn.execute(
            "UPDATE deadlines SET due_at=? WHERE case_id=?", (past, case_id))

    def test_timely_evidence_is_version_one(self) -> None:
        case = self._new_case()
        self._supplement_window(case["case_no"])
        _, ev = self.svc.submit_evidence(
            self.school_a, case["case_no"],
            {"title": "资源安排明细", "content_ref": "blob://on-time"}, None)
        self.assertTrue(ev["on_time"])
        self.assertEqual(ev["version_no"], 1)
        self.assertIsNone(ev["late_reason"])

    def test_late_material_only_enters_new_version(self) -> None:
        case = self._new_case()
        self._supplement_window(case["case_no"])
        _, first = self.svc.submit_evidence(
            self.school_a, case["case_no"],
            {"title": "首批材料", "content_ref": "blob://v1"}, None)
        self._expire_deadline(case["id"])
        _, late = self.svc.submit_evidence(
            self.school_a, case["case_no"],
            {"title": "逾期补交邮件", "content_ref": "blob://late"}, None)
        self.assertFalse(late["on_time"])
        self.assertEqual(late["version_no"], first["version_no"] + 1)
        self.assertIn("新版本", late["late_reason"])
        # 逾期材料绝不覆盖或改写既有版本
        self.assertTrue(self.svc.submit_evidence(
            self.school_a, case["case_no"],
            {"title": "再次逾期", "content_ref": "blob://late2"}, None)[1]["on_time"]
            is False)
        versions = self.svc.case_detail(self.school_a, case["case_no"])[
            "evidence_versions"]
        self.assertEqual([v["version_no"] for v in versions], [1, 2, 3])
        self.assertEqual(versions[0]["content_ref"], "blob://v1")

    # ------------------------------------------------------------ 利益回避

    def test_recused_original_reviewer_cannot_be_assigned(self) -> None:
        case = self._new_case()
        self.svc.accept_case(self.sec, case["case_no"], None)
        self.svc.declare_recusal(
            self.sec, case["case_no"],
            {"expert_username": "orig", "reason": "原评审人，存在利害关系"}, None)
        with self.assertRaises(PermissionError):
            self.svc.assign_reviewer(
                self.sec, case["case_no"], {"expert_username": "orig"}, None)

    def test_declaring_recusal_removes_existing_reviewer(self) -> None:
        case = self._new_case()
        self.svc.accept_case(self.sec, case["case_no"], None)
        self.svc.assign_reviewer(
            self.sec, case["case_no"], {"expert_username": "expert1"}, None)
        self.svc.declare_recusal(
            self.sec, case["case_no"],
            {"expert_username": "expert1", "reason": "发现亲属关系"}, None)
        detail = self.svc.case_detail(self.sec, case["case_no"])
        self.assertEqual(detail["reviewers"], [])

    def test_expert_can_only_declare_own_recusal(self) -> None:
        case = self._new_case()
        with self.assertRaises(PermissionError):
            self.svc.declare_recusal(
                self.e1, case["case_no"],
                {"expert_username": "expert2", "reason": "代声明"}, None)

    # ------------------------------------------------------------ 法定签署

    def _panel(self, case_no: str) -> list[str]:
        for name in ("expert1", "expert2", "expert3"):
            self.svc.assign_reviewer(
                self.sec, case_no, {"expert_username": name}, None)
        return ["expert1", "expert2", "expert3"]

    def test_decision_requires_legal_signature_count(self) -> None:
        case = self._new_case()
        self.svc.accept_case(self.sec, case["case_no"], None)
        self.svc.assign_reviewer(
            self.sec, case["case_no"], {"expert_username": "expert1"}, None)
        with self.assertRaises(QuorumError):
            self.svc.create_decision(
                self.sec, case["case_no"],
                {"ruling": "uphold", "body": "维持", "signer_ids": ["expert1"]},
                None)

    def test_decision_with_three_clean_signers_succeeds(self) -> None:
        case = self._new_case()
        self.svc.accept_case(self.sec, case["case_no"], None)
        panel = self._panel(case["case_no"])
        _, decision = self.svc.create_decision(
            self.sec, case["case_no"],
            {"ruling": "modify", "body": "调整资源安排",
             "original_outcome": "原分类为应用型", "signer_ids": panel}, None)
        self.assertTrue(decision["quorum_met"])
        self.assertEqual(decision["signed_count"], 3)
        self.assertEqual(
            self.svc.case_detail(self.sec, case["case_no"])["status"], "decided")

    def test_recused_signer_blocks_decision(self) -> None:
        case = self._new_case()
        self.svc.accept_case(self.sec, case["case_no"], None)
        for name in ("orig", "expert2", "expert3"):
            self.svc.assign_reviewer(
                self.sec, case["case_no"], {"expert_username": name}, None)
        self.svc.declare_recusal(
            self.sec, case["case_no"],
            {"expert_username": "orig", "reason": "原评审人必须退出"}, None)
        with self.assertRaises(PermissionError):
            self.svc.create_decision(
                self.sec, case["case_no"],
                {"ruling": "uphold", "body": "维持",
                 "signer_ids": ["orig", "expert2", "expert3"]}, None)

    def test_duplicate_signature_rejected(self) -> None:
        case = self._new_case()
        self.svc.accept_case(self.sec, case["case_no"], None)
        self._panel(case["case_no"])
        self.svc.create_decision(
            self.sec, case["case_no"],
            {"ruling": "modify", "body": "变更", "signer_ids": [
                "expert1", "expert2", "expert3"]}, None)
        with self.assertRaises(ConflictError):
            self.svc.sign_decision(self.e1, case["case_no"], None)

    # ------------------------------------------------------------ 决定差异

    def test_institution_sees_own_decision_difference(self) -> None:
        case = self._new_case()
        self.svc.accept_case(self.sec, case["case_no"], None)
        panel = self._panel(case["case_no"])
        self.svc.create_decision(
            self.sec, case["case_no"],
            {"ruling": "modify", "body": "调整分类档次",
             "original_outcome": "维持应用型", "signer_ids": panel}, None)
        decisions = self.svc.own_decisions(self.school_a)["decisions"]
        self.assertEqual(len(decisions), 1)
        d = decisions[0]
        self.assertTrue(d["differs_from_original"])
        self.assertEqual(d["ruling_label"], "变更")
        self.assertEqual(d["original_outcome"], "维持应用型")

    def test_uphold_decision_marks_no_difference(self) -> None:
        case = self._new_case()
        self.svc.accept_case(self.sec, case["case_no"], None)
        panel = self._panel(case["case_no"])
        _, d = self.svc.create_decision(
            self.sec, case["case_no"],
            {"ruling": "uphold", "body": "维持", "signer_ids": panel}, None)
        self.assertFalse(d["differs_from_original"])

    # ------------------------------------------------------------ 院校隔离

    def test_institution_cannot_see_other_school_case(self) -> None:
        case = self._new_case(self.school_a)
        with self.assertRaises(NotFoundError):
            self.svc.case_detail(self.school_b, case["case_no"])
        listed = self.svc.list_cases(self.school_b)["cases"]
        self.assertEqual(listed, [])

    def test_institution_cannot_submit_evidence_elsewhere(self) -> None:
        case = self._new_case(self.school_a)
        with self.assertRaises(PermissionError):
            self.svc.submit_evidence(
                self.school_b, case["case_no"],
                {"title": "越权", "content_ref": "blob://x"}, None)

    def test_expert_outside_panel_cannot_see_case(self) -> None:
        case = self._new_case()
        with self.assertRaises(NotFoundError):
            self.svc.case_detail(self.e1, case["case_no"])

    # ------------------------------------------------------ 合并/撤回/重开

    def test_merge_requires_same_institution(self) -> None:
        a = self._new_case(self.school_a)
        _, b = self.svc.create_case(self.school_b, {"subject": "乙校案件"}, None)
        with self.assertRaises(PermissionError):
            self.svc.merge_cases(
                self.sec,
                {"source_case": a["case_no"], "target_case": b["case_no"]}, None)

    def test_merge_moves_evidence_and_blocks_source(self) -> None:
        a = self._new_case(self.school_a)
        b = self._new_case(self.school_a)
        self.svc.accept_case(self.sec, a["case_no"], None)
        self.svc.submit_evidence(
            self.school_a, a["case_no"],
            {"title": "A 的材料", "content_ref": "blob://a"}, None)
        _, merged = self.svc.merge_cases(
            self.sec,
            {"source_case": a["case_no"], "target_case": b["case_no"]}, "merge-1")
        self.assertEqual(merged["source"]["status"], "merged")
        target = self.svc.case_detail(self.school_a, b["case_no"])
        self.assertEqual(len(target["evidence_versions"]), 1)
        self.assertEqual(target["evidence_versions"][0]["content_ref"], "blob://a")
        with self.assertRaises(StateConflictError):
            self.svc.accept_case(self.sec, a["case_no"], None)
        # 幂等重放
        _, replay = self.svc.merge_cases(
            self.sec,
            {"source_case": a["case_no"], "target_case": b["case_no"]}, "merge-1")
        self.assertEqual(replay["source"]["id"], merged["source"]["id"])

    def test_institution_may_withdraw_own_open_case(self) -> None:
        case = self._new_case()
        _, view = self.svc.withdraw_case(
            self.school_a, case["case_no"], {"reason": "院校自主撤回"}, None)
        self.assertEqual(view["status"], "withdrawn")

    def test_institution_cannot_withdraw_other_case(self) -> None:
        case = self._new_case(self.school_a)
        with self.assertRaises(PermissionError):
            self.svc.withdraw_case(
                self.school_b, case["case_no"], {"reason": "越权"}, None)

    def test_reopen_requires_admin(self) -> None:
        case = self._new_case()
        self.svc.withdraw_case(
            self.school_a, case["case_no"], {"reason": "撤回"}, None)
        with self.assertRaises(PermissionError):
            self.svc.reopen_case(
                self.sec, case["case_no"], {"reason": "秘书尝试重开"}, None)
        _, view = self.svc.reopen_case(
            self.admin, case["case_no"], {"reason": "发现新证据，批准重开"}, None)
        self.assertEqual(view["status"], "reopened")

    def test_only_withdrawn_or_merged_can_reopen(self) -> None:
        case = self._new_case()
        with self.assertRaises(StateConflictError):
            self.svc.reopen_case(
                self.admin, case["case_no"], {"reason": "尚未终结"}, None)

    # ------------------------------------------------------------ 暂缓执行

    def test_stay_grant_and_lift(self) -> None:
        case = self._new_case()
        _, stay = self.svc.grant_stay(
            self.sec, case["case_no"], {"reason": "防止资源调整不可逆"}, "stay-1")
        self.assertTrue(stay["active"])
        with self.assertRaises(ConflictError):
            self.svc.grant_stay(
                self.sec, case["case_no"], {"reason": "重复暂缓"}, None)
        _, lifted = self.svc.lift_stay(self.sec, case["case_no"], None)
        self.assertFalse(lifted["active"])

    def test_withdrawn_case_cannot_stay(self) -> None:
        case = self._new_case()
        self.svc.withdraw_case(
            self.school_a, case["case_no"], {"reason": "撤回"}, None)
        with self.assertRaises(StateConflictError):
            self.svc.grant_stay(
                self.sec, case["case_no"], {"reason": "x"}, None)

    def test_institution_cannot_grant_stay(self) -> None:
        case = self._new_case()
        with self.assertRaises(PermissionError):
            self.svc.grant_stay(
                self.school_a, case["case_no"], {"reason": "自我暂缓"}, None)

    # ------------------------------------------------------------ 状态与审计

    def test_accept_only_from_submitted(self) -> None:
        case = self._new_case()
        self.svc.accept_case(self.sec, case["case_no"], None)
        with self.assertRaises(StateConflictError):
            self.svc.accept_case(self.sec, case["case_no"], None)

    def test_audit_log_admin_only_and_leaks_no_material(self) -> None:
        case = self._new_case()
        self.svc.accept_case(self.sec, case["case_no"], None)
        self.svc.submit_evidence(
            self.school_a, case["case_no"],
            {"title": "敏感材料", "content_ref": "blob://secret-123"}, None)
        with self.assertRaises(PermissionError):
            self.svc.audit_log(self.sec)
        log = self.svc.audit_log(self.admin)
        self.assertGreaterEqual(log["total"], 2)
        joined = " ".join(e["detail"] for e in log["entries"])
        self.assertNotIn("blob://secret-123", joined)
        self.assertTrue(any(e["action"] == "accept" for e in log["entries"]))


if __name__ == "__main__":
    unittest.main()
