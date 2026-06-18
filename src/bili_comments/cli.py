from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .client import BilibiliWebSource
from .database import Database
from .errors import BiliCommentsError
from .exporter import ENTITY_FIELDS, export_records
from .service import crawl_target, parse_bvid


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="匿名采集 B站视频及评论")
    parser.add_argument(
        "--db", type=Path, default=Path("data/comments.db"), help="SQLite 数据库路径"
    )
    parser.add_argument("--verbose", action="store_true", help="输出请求级调试日志")
    subparsers = parser.add_subparsers(dest="command", required=True)

    crawl = subparsers.add_parser("crawl", help="抓取一个或多个视频")
    crawl.add_argument("targets", nargs="+", help="BV 号或视频 URL")
    crawl.add_argument("--order", choices=("hot", "time"), default="hot")
    crawl.add_argument("--replies", choices=("root", "all"), default="root")

    export = subparsers.add_parser("export", help="导出已保存的数据")
    export.add_argument("target", help="BV 号或视频 URL")
    export.add_argument("--entity", choices=tuple(ENTITY_FIELDS), default="comments")
    export.add_argument("--format", choices=("csv", "jsonl"), required=True)
    export.add_argument("--output", type=Path, required=True)

    inspect = subparsers.add_parser("inspect", help="查看本地视频和抓取状态")
    inspect.add_argument("target", help="BV 号或视频 URL")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        with Database(args.db) as database:
            if args.command == "crawl":
                with BilibiliWebSource() as source:
                    for target in args.targets:
                        bvid, count = crawl_target(
                            target,
                            source,
                            database,
                            comment_order=args.order,
                            replies_mode=args.replies,
                        )
                        print(f"{bvid}: 抓取完成，本次遍历 {count} 条评论")
            elif args.command == "export":
                bvid = parse_bvid(args.target)
                if not database.has_video(bvid):
                    raise BiliCommentsError(f"数据库中没有视频 {bvid}，请先执行 crawl")
                count = export_records(
                    database, bvid, args.entity, args.format, args.output
                )
                print(f"{bvid}: 已导出 {count} 条 {args.entity} 到 {args.output}")
            else:
                bvid = parse_bvid(args.target)
                details = database.inspect_video(bvid)
                if details is None:
                    raise BiliCommentsError(f"数据库中没有视频 {bvid}，请先执行 crawl")
                print(json.dumps(details, ensure_ascii=False, indent=2))
        return 0
    except (BiliCommentsError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
