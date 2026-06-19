from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from .database import Database
from .errors import BiliCommentsError

ENTITY_FIELDS = {
    "comments": (
        "rpid",
        "bvid",
        "message",
        "created_at",
        "like_count",
        "reply_count",
        "hot_rank",
        "sort_order",
        "sort_rank",
        "root_rpid",
        "parent_rpid",
        "level",
        "pin_type",
        "state",
        "author_mid",
        "author_name",
        "author_level",
        "first_seen_at",
        "last_seen_at",
    ),
    "video-stats": (
        "run_id",
        "bvid",
        "view_count",
        "danmaku_count",
        "reply_count",
        "favorite_count",
        "coin_count",
        "share_count",
        "like_count",
        "current_rank",
        "historical_rank",
        "observed_at",
    ),
    "comment-stats": (
        "run_id",
        "bvid",
        "rpid",
        "like_count",
        "reply_count",
        "sort_order",
        "sort_rank",
        "state",
        "observed_at",
    ),
}
EXPORT_FIELDS = ENTITY_FIELDS["comments"]


def _record(row: Mapping[str, object], entity: str) -> dict[str, object]:
    item = dict(row)
    if entity == "comments":
        item["created_at"] = datetime.fromtimestamp(
            int(item.pop("ctime")), timezone.utc
        ).isoformat(timespec="seconds")
    return {field: item[field] for field in ENTITY_FIELDS[entity]}


def _rows(database: Database, bvid: str, entity: str) -> Iterable[Mapping[str, object]]:
    if entity == "comments":
        return database.iter_comments(bvid)
    if entity == "video-stats":
        return database.iter_video_observations(bvid)
    if entity == "comment-stats":
        return database.iter_comment_observations(bvid)
    raise ValueError(f"unsupported export entity: {entity}")


def export_records(
    database: Database,
    bvid: str,
    entity: str,
    format_name: str,
    output: Path,
) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = _rows(database, bvid, entity)
    count = 0
    if format_name == "csv":
        with output.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=ENTITY_FIELDS[entity])
            writer.writeheader()
            for row in rows:
                writer.writerow(_record(row, entity))
                count += 1
    elif format_name == "jsonl":
        with output.open("w", encoding="utf-8", newline="\n") as file:
            for row in rows:
                file.write(json.dumps(_record(row, entity), ensure_ascii=False) + "\n")
                count += 1
    elif format_name == "parquet":
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as exc:
            raise BiliCommentsError(
                'Parquet 导出需要可选依赖，请执行 pip install -e ".[parquet]"'
            ) from exc
        records = [_record(row, entity) for row in rows]
        if records:
            table = pa.Table.from_pylist(records)
        else:
            table = pa.Table.from_pydict(
                {field: [] for field in ENTITY_FIELDS[entity]}
            )
        pq.write_table(table, output)
        count = len(records)
    else:
        raise ValueError(f"unsupported export format: {format_name}")
    return count


def export_comments(
    database: Database, bvid: str, format_name: str, output: Path
) -> int:
    """Compatibility wrapper for the v0.1 export function."""
    return export_records(database, bvid, "comments", format_name, output)
