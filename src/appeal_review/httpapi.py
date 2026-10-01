"""基于标准库的 JSON/HTTP 适配层。

- 认证：``Authorization: Bearer <token>``。
- 安全重试：写接口读取 ``Idempotency-Key`` 头，同键重试回放首次响应。
- 院校行级隔离在服务层完成；本层只做路由与 JSON 编解码。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .errors import AppError, MethodNotAllowed, NotFoundError, PermissionDenied, ValidationError
from .service import Service
from .store import Store


def _json_default(value):
    return str(value)


class Handler(BaseHTTPRequestHandler):
    server_version = "AppealReview/1.0"

    # -- 工具 ----------------------------------------------------------------

    def _send(self, status: int, body: Any) -> None:
        payload = json.dumps(body, ensure_ascii=False, default=_json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        try:
            raw = self.rfile.read(length).decode("utf-8")
            value = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"请求体不是合法 JSON：{exc}")
        if not isinstance(value, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return value

    def _auth(self):
        header = self.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else None
        return self.server.service.authenticate(token)

    def _idempotency_key(self) -> str | None:
        key = self.headers.get("Idempotency-Key")
        if key is not None and not key.strip():
            raise ValidationError("Idempotency-Key 不能为空")
        return key.strip() if key else None

    def log_message(self, fmt: str, *args) -> None:  # 安静：审计以库内日志为准
        if getattr(self.server, "http_log", False):
            super().log_message(fmt, *args)

    # -- 路由 ----------------------------------------------------------------

    CASE = re.compile(r"^/cases/([^/]+)$")

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            parts = urlsplit(self.path)
            path, query = parts.path.rstrip("/") or "/", parse_qs(parts.query)
            if path == "/health":
                return self._send(200, {"status": "ok"})
            body = self._read_json() if method == "POST" else {}
            if method == "GET" and path == "/cases":
                auth = self._auth()
                return self._send(200, {"cases": self.server.service.list_cases(auth)})
            if method == "POST" and path == "/users":
                return self._create_user(body)
            if method == "GET" and path == "/audit":
                auth = self._auth()
                case_id = (query.get("case_id") or [None])[0]
                return self._send(200, {"entries": self.server.service.list_audit(auth, case_id)})

            m = self.CASE.match(path)
            if m:
                return self._case_action(method, m.group(1), "", body)
            m = re.match(r"^/cases/([^/]+)/(.+)$", path)
            if m:
                return self._case_action(method, m.group(1), "/" + m.group(2), body)
            m = re.fullmatch(r"/withdrawal-requests/(\d+)/decision", path)
            if m and method == "POST":
                return self._send(200, self.server.service.decide_withdrawal(
                    self._auth(), int(m.group(1)), bool(body.get("approve", False))))
            raise NotFoundError("未知接口")
        except AppError as exc:
            self._send(exc.status, exc.to_dict())
        except Exception as exc:  # noqa: BLE001
            self._send(500, {"error": "internal_error", "message": str(exc)})

    def _create_user(self, body: dict) -> None:
        auth = self._auth()
        result = self.server.service.admin_create_user(
            auth, str(body.get("id") or "").strip(), str(body.get("role") or ""),
            str(body.get("name") or "").strip(), body.get("institution_id"),
            body.get("token"), idempotency_key=self._idempotency_key())
        self._send(201, result)

    def _case_action(self, method: str, case_id: str, action: str, body: dict) -> None:
        svc: Service = self.server.service
        if method != "POST" and action:
            raise MethodNotAllowed("该接口仅支持 POST")
        key = self._idempotency_key() if method == "POST" else None

        if action == "":
            if method == "GET":
                return self._send(200, svc.get_case(self._auth(), case_id))
            if method == "POST":
                return self._send(201, svc.submit_appeal(self._auth(),
                                                         {"case_id": case_id, **body},
                                                         idempotency_key=key))
            raise MethodNotAllowed("方法不支持")

        auth = self._auth()
        if action == "/accept":
            return self._send(200, svc.accept_appeal(auth, case_id))
        if action == "/accept-overdue":
            return self._send(200, svc.accept_appeal_overdue(auth, case_id, body.get("reason", "")))
        if action == "/supplement-rounds":
            return self._send(201, svc.open_supplement_round(
                auth, case_id, body.get("reason", ""), body.get("days")))
        if action == "/documents":
            result = svc.submit_documents(auth, case_id, body, idempotency_key=key)
            return self._send(201, result)
        if action == "/recusals":
            return self._send(201, svc.declare_recusal(
                auth, case_id, body.get("expert_id", ""), body.get("reason", "")))
        if action == "/original-reviewer":
            return self._send(201, svc.mark_original_reviewer(auth, case_id, body.get("expert_id", "")))
        if action == "/panel":
            return self._send(201, svc.assign_panelist(auth, case_id, body.get("expert_id", "")))
        if action == "/review/start":
            return self._send(200, svc.start_review(auth, case_id))
        if action == "/sign":
            return self._send(200, svc.sign(auth, case_id))
        if action == "/decisions":
            return self._send(201, svc.make_decision(
                auth, case_id, body.get("outcome", ""), body.get("rationale", "")))
        if action == "/stays":
            return self._send(201, svc.grant_stay(auth, case_id, body.get("reason", "")))
        m = re.fullmatch(r"/stays/(\d+)/lift", action)
        if m:
            return self._send(200, svc.lift_stay(auth, case_id, int(m.group(1))))
        if action == "/withdrawal":
            return self._send(201, svc.request_withdrawal(auth, case_id, body.get("reason", "")))
        if action == "/merge-into":
            return self._send(200, svc.merge_cases(auth, case_id, body.get("parent_id", "")))
        if action == "/reopen":
            return self._send(200, svc.reopen_case(auth, case_id, body.get("reason", "")))
        raise NotFoundError("未知操作")


class ApiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, service: Service, http_log: bool = False) -> None:
        self.service = service
        self.http_log = http_log
        super().__init__(addr, Handler)


def build_server(db_path: str, host: str = "127.0.0.1", port: int = 8080,
                 http_log: bool = False) -> ApiServer:
    store = Store(db_path)
    service = Service(store)
    return ApiServer((host, port), service, http_log=http_log)
