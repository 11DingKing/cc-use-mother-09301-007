"""统一的应用层错误，携带 HTTP 状态码与机器可读错误码。"""
from __future__ import annotations


class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        self.details = details or {}

    def to_dict(self) -> dict:
        result = {"error": self.code, "message": self.message}
        if self.details:
            result["details"] = self.details
        return result


class ValidationError(AppError):
    status = 400
    code = "invalid_request"


class PermissionDenied(AppError):
    status = 403
    code = "forbidden"


class NotFoundError(AppError):
    status = 404
    code = "not_found"


class ConflictError(AppError):
    status = 409
    code = "conflict"


class StateError(ConflictError):
    code = "illegal_state"


class MethodNotAllowed(AppError):
    status = 405
    code = "method_not_allowed"
