"""命令行入口：``python -m appeal_review.serve --db data/appeals.db``。"""
from __future__ import annotations

import argparse

from .httpapi import build_server


def main() -> None:
    parser = argparse.ArgumentParser(description="分类改革申诉复核后端")
    parser.add_argument("--db", default="data/appeals.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--http-log", action="store_true")
    args = parser.parse_args()

    server = build_server(args.db, args.host, args.port, http_log=args.http_log)
    print(f"服务已启动：http://{args.host}:{args.port} （数据库 {args.db}）")
    print(f"管理员创建令牌示例（POST /users 需已有管理员令牌；首次部署请用 seed 脚本）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
