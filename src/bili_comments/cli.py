from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .client import BiliClient
from .database import Database
from .errors import BiliCommentsError
from .exporter import export_comments
from .service import crawl_target, parse_bvid


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="匿名采集 B站视频一级评论")
    parser.add_argument("--db", type=Path, default=Path("data/comments.db"), help="SQLite 数据库路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl = subparsers.add_parser("crawl", help="抓取一个或多个视频")
    crawl.add_argument("targets", nargs="+", help="BV 号或视频 URL")

    export = subparsers.add_parser("export", help="导出已保存的评论")
    export.add_argument("target", help="BV 号或视频 URL")
    export.add_argument("--format", choices=("csv", "jsonl"), required=True)
    export.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with Database(args.db) as database:
            if args.command == "crawl":
                with BiliClient() as client:
                    for target in args.targets:
                        bvid, count = crawl_target(target, client, database)
                        print(f"{bvid}: 抓取完成，本次遍历 {count} 条一级评论")
            else:
                bvid = parse_bvid(args.target)
                if not database.has_video(bvid):
                    raise BiliCommentsError(f"数据库中没有视频 {bvid}，请先执行 crawl")
                count = export_comments(database, bvid, args.format, args.output)
                print(f"{bvid}: 已导出 {count} 条评论到 {args.output}")
        return 0
    except (BiliCommentsError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
