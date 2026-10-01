"""幂等提交与安全重试。"""
from __future__ import annotations

import threading
import unittest

from backend_helper import BackendTest
from appeal_review.errors import ConflictError


class IdempotencyTest(BackendTest):
    def test_retry_with_same_key_replays_without_duplicate(self) -> None:
        first = self.submit_simple(self.school_a, "C-1", key="key-submit-1")
        # 网络重试：同样的载荷、同样的键，必须原样回放且不产生第二条案件。
        second = self.submit_simple(self.school_a, "C-1", key="key-submit-1")
        self.assertEqual(first["id"], second["id"])
        count = self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) AS n FROM cases WHERE id='C-1'").fetchone()["n"])
        self.assertEqual(count, 1)

    def test_same_key_different_operation_is_distinct(self) -> None:
        self.submit_simple(self.school_a, "C-1", key="k")
        # 幂等键按操作类型命名空间隔离：提交用 k 不影响补证用 k。
        self.svc.accept_appeal(self.secretary, "C-1")
        self.svc.open_supplement_round(self.secretary, "C-1", "补", 5)
        r1 = self.svc.submit_documents(
            self.school_a, "C-1", {"documents": [self.doc(h="h2")]}, idempotency_key="k")
        self.assertEqual(r1["submission"]["version"], 2)

    def test_document_retry_does_not_create_new_version(self) -> None:
        self.submit_simple(self.school_a, "C-1", key="k1")
        self.svc.accept_appeal(self.secretary, "C-1")
        self.svc.open_supplement_round(self.secretary, "C-1", "补", 5)
        payload = {"documents": [self.doc("a.pdf", "hx")]}
        r1 = self.svc.submit_documents(self.school_a, "C-1", payload, idempotency_key="doc-k")
        r2 = self.svc.submit_documents(self.school_a, "C-1", payload, idempotency_key="doc-k")
        self.assertEqual(r1["submission"]["version"], r2["submission"]["version"])
        docs = self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) AS n FROM documents WHERE case_id='C-1'").fetchone()["n"])
        self.assertEqual(docs, 2)  # 初始 1 + 补证 1

    def test_replay_returns_first_response_even_after_state_changes(self) -> None:
        self.submit_simple(self.school_a, "C-1", key="k1")
        self.svc.accept_appeal(self.secretary, "C-1")
        # 重放提交时返回的是首次响应（state=submitted），而非当前状态。
        replay = self.submit_simple(self.school_a, "C-1", key="k1")
        self.assertEqual(replay["state"], "submitted")


class ConcurrencyTest(BackendTest):
    def test_concurrent_writes_to_distinct_cases_both_succeed(self) -> None:
        results: list[Exception] = []

        def submit(cid: str) -> None:
            try:
                self.submit_simple(self.school_a, cid, key=f"key-{cid}")
            except Exception as exc:  # noqa: BLE001
                results.append(exc)

        threads = [threading.Thread(target=submit, args=(f"C-{i}",)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results, [])
        n = self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) AS n FROM cases").fetchone()["n"])
        self.assertEqual(n, 8)

    def test_unique_case_id_conflict_rejected(self) -> None:
        self.submit_simple(self.school_a, "C-DUP", key="a")
        with self.assertRaises(ConflictError):
            self.submit_simple(self.school_a, "C-DUP", key="b")

    def test_rollback_on_failure_leaves_no_partial_writes(self) -> None:
        def bad(conn, now):
            conn.execute(
                "INSERT INTO cases (id, institution_id, title, state, accept_due) "
                "VALUES ('X','SCHOOL-A','t','submitted',?)", (now,))
            raise RuntimeError("故意失败")
        with self.assertRaises(RuntimeError):
            self.store.write(bad, operation="x", idempotency_key=None, user_id="sec-1")
        n = self.store.read(lambda c: c.execute(
            "SELECT COUNT(*) AS n FROM cases").fetchone()["n"])
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
