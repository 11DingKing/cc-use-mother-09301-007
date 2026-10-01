"""端到端 HTTP 测试：鉴权、幂等重放、隔离与错误码。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from appeal_review.api import make_handler  # noqa: E402
from appeal_review.service import AppealService  # noqa: E402


class HttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = AppealService(":memory:")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.svc))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def _request(self, method: str, path: str, token: str | None = None,
                 body: dict | None = None, idem: str | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if idem:
            req.add_header("Idempotency-Key", idem)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def _login(self, username: str) -> str:
        status, body = self._request("POST", "/api/auth/login",
                                     body={"username": username})
        self.assertEqual(status, 200)
        return body["token"]

    def test_full_workflow_over_http(self) -> None:
        school = self._login("school_a")
        sec = self._login("sec")
        admin = self._login("admin")

        # 幂等提交：相同键重放只建一个案件
        status, first = self._request(
            "POST", "/api/cases", school,
            {"subject": "分类评价导致经费下调"}, "case-1")
        self.assertEqual(status, 201)
        status, replay = self._request(
            "POST", "/api/cases", school,
            {"subject": "分类评价导致经费下调"}, "case-1")
        self.assertEqual(status, 201)
        self.assertEqual(first["id"], replay["id"])
        case_no = first["case_no"]

        self.assertEqual(self._request("POST", f"/api/cases/{case_no}/accept",
                                       sec, {}, "acc-1")[0], 200)
        self.assertEqual(self._request("POST", f"/api/cases/{case_no}/supplement",
                                       sec, {"days": 10}, "sup-1")[0], 201)
        status, ev = self._request(
            "POST", f"/api/cases/{case_no}/evidence", school,
            {"title": "经费明细", "content_ref": "blob://budget"}, "ev-1")
        self.assertEqual(status, 201)
        self.assertTrue(ev["on_time"])

        # 原评审人回避后不得进入复核名单
        self.assertEqual(self._request(
            "POST", f"/api/cases/{case_no}/recusals", sec,
            {"expert_username": "orig", "reason": "参与原评审"}, "rec-1")[0], 201)
        status, err = self._request(
            "POST", f"/api/cases/{case_no}/reviewers", sec,
            {"expert_username": "orig"})
        self.assertEqual(status, 403)
        self.assertEqual(err["error"], "FORBIDDEN")

        for i, name in enumerate(("expert1", "expert2", "expert3")):
            self.assertEqual(self._request(
                "POST", f"/api/cases/{case_no}/reviewers", sec,
                {"expert_username": name}, f"rev-{i}")[0], 201)

        # 签署人数不足 -> 409 QUORUM_NOT_MET
        status, err = self._request(
            "POST", f"/api/cases/{case_no}/decision", sec,
            {"ruling": "modify", "body": "调整档次",
             "signer_ids": ["expert1", "expert2"]})
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "QUORUM_NOT_MET")

        status, decision = self._request(
            "POST", f"/api/cases/{case_no}/decision", sec,
            {"ruling": "modify", "body": "调整档次",
             "original_outcome": "原应用型",
             "signer_ids": ["expert1", "expert2", "expert3"]}, "dec-1")
        self.assertEqual(status, 201)
        self.assertTrue(decision["quorum_met"])

        # 暂缓执行 + 解除（决定后仍可采取执行措施）
        self.assertEqual(self._request(
            "POST", f"/api/cases/{case_no}/stays", sec,
            {"reason": "等待资源拨付调整"}, "stay-1")[0], 201)
        self.assertEqual(self._request(
            "DELETE", f"/api/cases/{case_no}/stays", sec)[0], 200)

        # 院校看到与原结果的差异
        status, own = self._request("GET", "/api/decisions/own", school)
        self.assertEqual(status, 200)
        self.assertTrue(own["decisions"][0]["differs_from_original"])

        # 管理员审计不泄露材料指针
        status, audit = self._request("GET", "/api/audit", admin)
        self.assertEqual(status, 200)
        self.assertNotIn("blob://budget", json.dumps(audit, ensure_ascii=False))

    def test_auth_required_and_cross_school_isolation(self) -> None:
        school_a = self._login("school_a")
        school_b = self._login("school_b")
        self.assertEqual(self._request("GET", "/api/cases")[0], 403)

        status, case = self._request(
            "POST", "/api/cases", school_a, {"subject": "甲校密案"}, "a-1")
        self.assertEqual(status, 201)
        # 乙校访问 -> 404，不暴露案件存在
        status, _ = self._request("GET", f"/api/cases/{case['case_no']}", school_b)
        self.assertEqual(status, 404)

    def test_idempotency_replay_is_safe_after_conflict(self) -> None:
        school = self._login("school_a")
        sec = self._login("sec")
        status, case = self._request(
            "POST", "/api/cases", school, {"subject": "重试验证"}, "k-1")
        self.assertEqual(status, 201)
        # 受理一次
        self.assertEqual(self._request(
            "POST", f"/api/cases/{case['case_no']}/accept", sec, {}, "acc")[0], 200)
        # 相同幂等键安全重试 -> 仍然 200，状态不被破坏
        status, replay = self._request(
            "POST", f"/api/cases/{case['case_no']}/accept", sec, {}, "acc")
        self.assertEqual(status, 200)
        self.assertEqual(replay["status"], "accepted")
        # 同键换请求体 -> 409
        status, err = self._request(
            "POST", f"/api/cases/{case['case_no']}/supplement", sec,
            {"days": 5}, "acc")
        self.assertEqual(status, 409)
        self.assertEqual(err["error"], "IDEMPOTENCY_KEY_REUSED")


if __name__ == "__main__":
    unittest.main()
