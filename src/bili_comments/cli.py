from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .batch import resume_batch, run_manifest
from .client import BilibiliWebSource
from .database import Database
from .errors import BiliCommentsError, ConfigurationError
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
    export.add_argument("--format", choices=("csv", "jsonl", "parquet"), required=True)
    export.add_argument("--output", type=Path, required=True)

    inspect = subparsers.add_parser("inspect", help="查看本地视频和抓取状态")
    inspect.add_argument("target", help="BV 号或视频 URL")

    batch = subparsers.add_parser("batch", help="运行和恢复批量采集")
    batch_commands = batch.add_subparsers(dest="batch_command", required=True)
    batch_run = batch_commands.add_parser("run", help="从 CSV 创建并运行批次")
    batch_run.add_argument("manifest", type=Path, help="CSV 目标清单")
    batch_run.add_argument("--summary", type=Path, help="JSON 摘要输出路径")
    batch_resume = batch_commands.add_parser("resume", help="恢复指定批次")
    batch_resume.add_argument("batch_id", type=int)
    batch_resume.add_argument("--summary", type=Path, help="JSON 摘要输出路径")
    batch_status = batch_commands.add_parser("status", help="查看批次状态")
    batch_status.add_argument("batch_id", type=int, nargs="?")
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
            elif args.command == "inspect":
                bvid = parse_bvid(args.target)
                details = database.inspect_video(bvid)
                if details is None:
                    raise BiliCommentsError(f"数据库中没有视频 {bvid}，请先执行 crawl")
                print(json.dumps(details, ensure_ascii=False, indent=2))
            elif args.batch_command == "status":
                if args.batch_id is None:
                    details = {
                        "batches": [dict(row) for row in database.list_batch_runs()]
                    }
                else:
                    details = database.batch_details(args.batch_id)
                print(json.dumps(details, ensure_ascii=False, indent=2))
            else:
                with BilibiliWebSource() as source:
                    if args.batch_command == "run":
                        details, exit_code = run_manifest(
                            database,
                            source,
                            args.manifest,
                            summary_path=args.summary,
                        )
                    else:
                        details, exit_code = resume_batch(
                            database,
                            source,
                            args.batch_id,
                            summary_path=args.summary,
                        )
                batch_info = details["batch"]
                print(
                    f"批次 {batch_info['id']}: {batch_info['status']}，"
                    f"成功 {batch_info['succeeded_count']}，失败 {batch_info['failed_count']}，"
                    f"摘要 {batch_info['summary_path']}"
                )
                return exit_code
        return 0
    except ConfigurationError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except (BiliCommentsError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
