"""业务常量。所有时限以自然日计，存 UTC，比较时精确到秒。"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # 复核机构受理申诉的期限（自提交起）。
    accept_deadline_days: int = 5
    # 补证通知默认给出的期限。
    supplement_deadline_days: int = 10
    # 补证期限允许的上下限（天）。
    supplement_min_days: int = 1
    supplement_max_days: int = 60
    # 法定复核专家组人数。
    min_panel_size: int = 3
    # 法定签署人数（达到该数方可作出决定）。
    required_signatures: int = 3
    # 写入事务遇到锁时的重试次数与初始退避（秒）。
    write_attempts: int = 5
    retry_backoff_seconds: float = 0.02
