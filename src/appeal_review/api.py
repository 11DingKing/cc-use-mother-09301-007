"""HTTP 接口：仅依赖标准库，路由到 AppealService。

约定：
- 登录：POST /api/auth/login {"username": "..."} -> token
- 鉴权：Authorization: Bearer <token>
- 安全重试：写请求携带 Idempotency-Key: <键>，重放返回首次结果
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .errors import DomainError
from .service import AppealService


def make_handler(service: AppealService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AppealReview/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
            return

        # ------------------------------------------------------------ 工具

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _user(self):
            header = self.headers.get("Authorization", "")
            token = header[7:] if header.startswith("Bearer ") else None
            return service.authenticate(token)

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                raw = self.rfile.read(length)
                value = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise DomainError("请求体不是合法 JSON", code="VALIDATION_ERROR")
            if not isinstance(value, dict):
                raise DomainError("请求体必须是 JSON 对象", code="VALIDATION_ERROR")
            return value

        def _idem(self) -> str | None:
            return self.headers.get("Idempotency-Key")

        def _run_write(self, fn: Callable[[], tuple[int, Any]]) -> None:
            try:
                with service.guard():
                    status, body = fn()
                self._send(status, body)
            except DomainError as exc:
                self._send(exc.http_status,
                           {"error": exc.code, "message": exc.message})

        def _run_read(self, fn: Callable[[], Any]) -> None:
            try:
                with service.guard():
                    body = fn()
                self._send(200, body)
            except DomainError as exc:
                self._send(exc.http_status,
                           {"error": exc.code, "message": exc.message})

        # ------------------------------------------------------------ 路由

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/api/cases":
                self._run_read(lambda: service.list_cases(self._user()))
                return
            if path == "/api/decisions/own":
                self._run_read(lambda: service.own_decisions(self._user()))
                return
            if path == "/api/audit":
                query = self._query()
                self._run_read(lambda: service.audit_log(
                    self._user(), query.get("limit", 100), query.get("offset", 0)))
                return
            m = re.fullmatch(r"/api/cases/([^/]+)", path)
            if m:
                ref = m.group(1)
                self._run_read(lambda: service.case_detail(self._user(), ref))
                return
            self._send(404, {"error": "NOT_FOUND", "message": "未知接口"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            try:
                payload = self._read_json()
            except DomainError as exc:
                self._send(exc.http_status,
                           {"error": exc.code, "message": exc.message})
                return

            if path == "/api/auth/login":
                self._run_write(
                    lambda: (200, service.login(str(payload.get("username", "")))))
                return

            try:
                user = self._user()
            except DomainError as exc:
                self._send(exc.http_status,
                           {"error": exc.code, "message": exc.message})
                return
            key = self._idem()

            routes: dict[str, Callable[[], tuple[int, Any]]] = {
                "/api/cases": lambda: service.create_case(user, payload, key),
                "/api/merges": lambda: service.merge_cases(user, payload, key),
            }
            if path in routes:
                self._run_write(routes[path])
                return

            m = re.fullmatch(r"/api/cases/([^/]+)/(\w+)", path)
            if m:
                ref, action = m.group(1), m.group(2)
                actions: dict[str, Callable[[], tuple[int, Any]]] = {
                    "accept": lambda: service.accept_case(user, ref, key),
                    "supplement": lambda: service.request_supplement(
                        user, ref, payload, key),
                    "evidence": lambda: service.submit_evidence(
                        user, ref, payload, key),
                    "recusals": lambda: service.declare_recusal(
                        user, ref, payload, key),
                    "reviewers": lambda: service.assign_reviewer(
                        user, ref, payload, key),
                    "decision": lambda: service.create_decision(
                        user, ref, payload, key),
                    "sign": lambda: service.sign_decision(user, ref, key),
                    "stays": lambda: service.grant_stay(user, ref, payload, key),
                    "withdraw": lambda: service.withdraw_case(
                        user, ref, payload, key),
                    "reopen": lambda: service.reopen_case(
                        user, ref, payload, key),
                }
                if action in actions:
                    self._run_write(actions[action])
                    return
            self._send(404, {"error": "NOT_FOUND", "message": "未知接口"})

        def do_DELETE(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            try:
                user = self._user()
            except DomainError as exc:
                self._send(exc.http_status,
                           {"error": exc.code, "message": exc.message})
                return
            m = re.fullmatch(r"/api/cases/([^/]+)/stays", path)
            if m:
                ref = m.group(1)
                self._run_write(lambda: service.lift_stay(user, ref, self._idem()))
                return
            self._send(404, {"error": "NOT_FOUND", "message": "未知接口"})

        def _query(self) -> dict[str, str]:
            if "?" not in self.path:
                return {}
            from urllib.parse import parse_qs
            parsed = parse_qs(self.path.split("?", 1)[1])
            return {k: v[0] for k, v in parsed.items()}

    return Handler


def serve(db_path: str = "appeal_review.db", host: str = "127.0.0.1",
          port: int = 8080, **kwargs: Any) -> None:
    service = AppealService(db_path, **kwargs)
    httpd = ThreadingHTTPServer((host, port), make_handler(service))
    print(f"申诉复核后端已启动：http://{host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
