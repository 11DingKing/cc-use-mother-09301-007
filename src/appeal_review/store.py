"""存储基础设施：连接、时钟、事务化写入与安全重试。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

from . import schema
from .config import Settings
from .errors import ConflictError

T = TypeVar("T")


class Clock:
    """可替换时钟，生产用 UTC，测试可冻结/快进。"""

    def now_iso(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def monotonic(self) -> float:
        return time.monotonic()


class Store:
    """线程安全的 SQLite 存储。

    每个写事务使用独立的短连接并开启 ``IMMEDIATE``，配合退避重试，
    保证并发写入不会因延迟加锁而互相破坏；幂等键在同一事务内登记，
    客户重试只会回放首次响应而不会重复执行。
    """

    def __init__(self, path: str, settings: Settings | None = None, clock: Clock | None = None) -> None:
        self.path = path
        self.settings = settings or Settings()
        self.clock = clock or Clock()
        self._lock = threading.Lock()
        self._mem_conn: sqlite3.Connection | None = None
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
            with self._connect() as conn:
                schema.initialize(conn)
        else:
            # 内存库的每个连接互相隔离，因此常驻一个连接并用锁串行化。
            conn = self._connect()
            schema.initialize(conn)
            self._mem_conn = conn

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    # -- 读取 ----------------------------------------------------------------

    def read(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        if self._mem_conn is not None:
            with self._lock:
                return fn(self._mem_conn)
        with self._connect() as conn:
            return fn(conn)

    # -- 写入（带重试与幂等）-------------------------------------------------

    def write(
        self,
        fn: Callable[[sqlite3.Connection, str], T],
        *,
        operation: str,
        idempotency_key: str | None,
        user_id: str,
    ) -> T:
        """在事务内执行 ``fn(conn, now_iso)``。

        ``fn`` 必须只使用传入连接完成全部读写。提供 ``idempotency_key`` 时，
        同键重试回放首次结果；结果需可 JSON 序列化（HTTP 层会据此返回）。
        """
        attempts = self.settings.write_attempts
        delay = self.settings.retry_backoff_seconds
        last_error: Exception | None = None
        for _ in range(max(1, attempts)):
            try:
                return self._attempt(fn, operation, idempotency_key, user_id)
            except sqlite3.IntegrityError as exc:
                # 唯一约束/外键冲突属于确定性业务冲突，重试无意义。
                raise ConflictError(f"数据冲突：{exc}") from exc
            except sqlite3.OperationalError as exc:  # database is locked / busy
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    last_error = exc
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
        raise ConflictError("写入冲突，请稍后重试", details={"retryable": True}) from last_error

    def _attempt(self, fn, operation: str, idempotency_key: str | None, user_id: str):
        own_conn = self._mem_conn is None
        if own_conn:
            conn = self._connect()
        else:
            conn = self._mem_conn  # 内存库：同一连接，外层锁串行化
            self._lock.acquire()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = self.clock.now_iso()
            if idempotency_key is not None:
                row = conn.execute(
                    "SELECT response_body FROM idempotency_keys "
                    "WHERE operation=? AND idempotency_key=?",
                    (operation, idempotency_key),
                ).fetchone()
                if row is not None:
                    replay = {"__idempotent_replay__": json.loads(row["response_body"])}
                    conn.execute("COMMIT")
                    return replay
            result = fn(conn, now)
            if idempotency_key is not None:
                conn.execute(
                    "INSERT INTO idempotency_keys (idempotency_key, operation, user_id, response_body) "
                    "VALUES (?,?,?,?)",
                    (idempotency_key, operation, user_id, json.dumps(result, ensure_ascii=False)),
                )
            conn.execute("COMMIT")
            return result
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            if self._mem_conn is not None:
                self._lock.release()
            if own_conn:
                conn.close()

    # -- 审计 ----------------------------------------------------------------

    @staticmethod
    def audit(conn: sqlite3.Connection, actor_id: str | None, action: str, entity: str,
              entity_id: str | None, detail: dict | None = None) -> None:
        conn.execute(
            "INSERT INTO audit_log (actor_id, action, entity, entity_id, detail_json) VALUES (?,?,?,?,?)",
            (actor_id, action, entity, entity_id, json.dumps(detail or {}, ensure_ascii=False)),
        )
