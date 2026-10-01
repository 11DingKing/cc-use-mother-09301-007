"""分类改革申诉复核后端。

模块划分：

- ``config``：时限与法定人数等可配置常量。
- ``schema``：SQLite 表结构与只增触发器。
- ``store``：连接管理、事务重试等写入安全基础设施。
- ``service``：状态机、权限、回避、版本与决定差异等领域规则。
- ``httpapi``：基于标准库 ``http.server`` 的 JSON/HTTP 适配层。
"""
from .config import Settings
from .errors import AppError, ConflictError, NotFoundError, PermissionDenied, StateError, ValidationError
from .schema import SCHEMA_VERSION
from .service import AuthContext, Service
from .store import Store

__all__ = [
    "Settings",
    "AppError",
    "ConflictError",
    "NotFoundError",
    "PermissionDenied",
    "StateError",
    "ValidationError",
    "SCHEMA_VERSION",
    "Store",
    "Service",
    "AuthContext",
]
