"""首次部署的种子脚本：直接在数据库上创建一个管理员账户。

用法：``python -m appeal_review.seed --db data/appeals.db``
输出新管理员的登录令牌（仅显示一次）。
"""
from __future__ import annotations

import argparse
import secrets

from .service import Service
from .store import Store


def seed_admin(db: str, name: str = "初始管理员") -> tuple[str, str]:
    store = Store(db)
    service = Service(store)
    admin_id = "admin-root"
    token = secrets.token_urlsafe(24)

    def tx(conn, now):
        existing = conn.execute("SELECT token FROM users WHERE id=?", (admin_id,)).fetchone()
        if existing is not None:
            return {"id": admin_id, "token": existing["token"], "reused": True}
        service.create_user(conn, admin_id, "admin", name, None, token)
        return {"id": admin_id, "token": token, "reused": False}

    result = store.write(tx, operation="seed.admin", idempotency_key=None, user_id="system")
    return result["id"], result["token"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/appeals.db")
    args = parser.parse_args()
    admin_id, token = seed_admin(args.db)
    print(f"管理员：{admin_id}")
    print(f"令牌：{token}")


if __name__ == "__main__":
    main()
