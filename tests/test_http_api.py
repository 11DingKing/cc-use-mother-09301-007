"""HTTP 端到端：真实起服、令牌认证、幂等头、行级隔离。"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.request

from backend_helper import BackendTest
from appeal_review.httpapi import ApiServer
from appeal_review.service import Service


class HttpTest(BackendTest):
    def setUp(self) -> None:
        super().setUp()
        self.server = ApiServer(("127.0.0.1", 0), Service(self.store))
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def request(self, method: str, path: str, token: str | None = None,
                body: dict | None = None, idem: str | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.url(path), data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        if idem:
            req.add_header("Idempotency-Key", idem)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_health(self) -> None:
        status, body = self.request("GET", "/health")
        self.assertEqual((status, body["status"]), (200, "ok"))

    def test_unauthenticated_rejected(self) -> None:
        status, body = self.request("GET", "/cases")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "unauthorized")

    def test_full_flow_over_http(self) -> None:
        # 院校提交（幂等头）。
        status, case = self.request(
            "POST", "/cases/C-1", "tok-a",
            {"title": "申诉", "documents": [{"filename": "a.pdf",
                                            "content_hash": "h1", "size_bytes": 3}]},
            idem="submit-1")
        self.assertEqual(status, 201)
        self.assertEqual(case["state"], "submitted")

        # 重试同键返回相同结果。
        status2, case2 = self.request(
            "POST", "/cases/C-1", "tok-a",
            {"title": "申诉", "documents": [{"filename": "a.pdf",
                                            "content_hash": "h1", "size_bytes": 3}]},
            idem="submit-1")
        self.assertEqual(status2, 201)
        self.assertEqual(case2["id"], "C-1")

        # 秘书受理、组队、启动复核。
        self.assertEqual(self.request("POST", "/cases/C-1/accept", "tok-sec")[0], 200)
        for n in range(1, 4):
            self.assertEqual(
                self.request("POST", "/cases/C-1/panel", "tok-sec",
                             {"expert_id": f"exp-{n}"})[0], 201)
        self.assertEqual(self.request("POST", "/cases/C-1/review/start", "tok-sec")[0], 200)
        for n in range(1, 4):
            self.assertEqual(self.request("POST", "/cases/C-1/sign", f"tok-exp{n}")[0], 200)
        status, decided = self.request(
            "POST", "/cases/C-1/decisions", "tok-sec",
            {"outcome": "upheld", "rationale": "维持。"})
        self.assertEqual(status, 201)
        self.assertEqual(decided["state"], "decided")

    def test_cross_school_access_is_404_over_http(self) -> None:
        self.request("POST", "/cases/CA", "tok-a",
                     {"title": "x", "documents": [{"filename": "f",
                                                   "content_hash": "h", "size_bytes": 1}]})
        status, body = self.request("GET", "/cases/CA", "tok-b")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_quorum_shortage_surfaces_conflict(self) -> None:
        self.request("POST", "/cases/C-9", "tok-a",
                     {"title": "x", "documents": [{"filename": "f",
                                                   "content_hash": "h", "size_bytes": 1}]})
        self.request("POST", "/cases/C-9/accept", "tok-sec")
        self.request("POST", "/cases/C-9/panel", "tok-sec", {"expert_id": "exp-1"})
        self.request("POST", "/cases/C-9/panel", "tok-sec", {"expert_id": "exp-2"})
        self.request("POST", "/cases/C-9/panel", "tok-sec", {"expert_id": "exp-3"})
        self.request("POST", "/cases/C-9/review/start", "tok-sec")
        self.request("POST", "/cases/C-9/sign", "tok-exp1")
        status, body = self.request(
            "POST", "/cases/C-9/decisions", "tok-sec",
            {"outcome": "upheld", "rationale": "x"})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "quorum_unmet")


if __name__ == "__main__":
    unittest.main()
