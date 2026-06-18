from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from .database import Database

EXPORT_FIELDS = (
    "rpid",
    "bvid",
    "message",
    "created_at",
    "like_count",
    "reply_count",
    "hot_rank",
    "author_mid",
    "author_name",
    "author_level",
    "first_seen_at",
    "last_seen_at",
)


def _record(row: object) -> dict[str, object]:
    item = dict(row)  # type: ignore[arg-type]
    item["created_at"] = datetime.fromtimestamp(
        int(item.pop("ctime")), timezone.utc
    ).isoformat(timespec="seconds")
    return {field: item[field] for field in EXPORT_FIELDS}


def export_comments(database: Database, bvid: str, format_name: str, output: Path) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    rows = database.iter_comments(bvid)
    count = 0
    if format_name == "csv":
        with output.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=EXPORT_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow(_record(row))
                count += 1
    elif format_name == "jsonl":
        with output.open("w", encoding="utf-8", newline="\n") as file:
            for row in rows:
                file.write(json.dumps(_record(row), ensure_ascii=False) + "\n")
                count += 1
    else:
        raise ValueError(f"unsupported export format: {format_name}")
    return count
