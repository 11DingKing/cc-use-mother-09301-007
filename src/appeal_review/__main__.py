"""命令行入口：python3 -m appeal_review [--db PATH] [--host H] [--port P]"""
from __future__ import annotations

import argparse

from .api import serve


def main() -> None:
    parser = argparse.ArgumentParser(description="分类改革申诉复核后端")
    parser.add_argument("--db", default="appeal_review.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--supplement-days", type=int, default=10,
                        help="补证受理期限（天）")
    parser.add_argument("--required-signatures", type=int, default=3,
                        help="复核决定法定签署人数")
    args = parser.parse_args()
    serve(args.db, args.host, args.port,
          supplement_days=args.supplement_days,
          required_signatures=args.required_signatures)


if __name__ == "__main__":
    main()
