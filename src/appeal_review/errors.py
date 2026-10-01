"""领域错误与 HTTP 状态映射。"""
from __future__ import annotations


class DomainError(Exception):
    """所有可预期的业务规则违反。"""

    http_status = 422
    code = "RULE_VIOLATION"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ValidationError(DomainError):
    http_status = 400
    code = "VALIDATION_ERROR"


class AuthenticationError(DomainError):
    http_status = 401
    code = "UNAUTHENTICATED"


class PermissionError(DomainError):  # noqa: A001 - 领域内有意遮蔽内建名
    http_status = 403
    code = "FORBIDDEN"


class NotFoundError(DomainError):
    http_status = 404
    code = "NOT_FOUND"


class ConflictError(DomainError):
    http_status = 409
    code = "CONFLICT"


class DeadlinePassedError(ConflictError):
    code = "DEADLINE_PASSED"


class StateConflictError(ConflictError):
    code = "STATE_CONFLICT"


class QuorumError(ConflictError):
    code = "QUORUM_NOT_MET"


class IdempotencyConflictError(ConflictError):
    code = "IDEMPOTENCY_KEY_REUSED"
