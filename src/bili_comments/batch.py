from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path

from .database import Database
from .errors import BiliCommentsError, ConfigurationError
from .service import crawl_target, parse_bvid
from .source import SourceAdapter

TRUE_VALUES = {"", "1", "true", "yes", "on"}
FALSE_VALUES = {"0", "false", "no", "off"}


@dataclass(frozen=True)
class ManifestItem:
    row_number: int
    target: str
    bvid: str
    comment_order: str
    replies_mode: str

    def database_values(self) -> tuple[int, str, str, str, str]:
        return (
            self.row_number,
            self.target,
            self.bvid,
            self.comment_order,
            self.replies_mode,
        )


@dataclass(frozen=True)
class Manifest:
    path: Path
    sha256: str
    items: tuple[ManifestItem, ...]


def _enabled(value: object, row_number: int) -> bool:
    normalized = str(value or "").strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ConfigurationError(
        f"CSV 第 {row_number} 行 enabled 值无效：{value!s}"
    )


def parse_manifest(path: str | Path) -> Manifest:
    manifest_path = Path(path).resolve()
    try:
        raw = manifest_path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(f"无法读取目标清单：{manifest_path}") from exc
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("目标清单必须使用 UTF-8 编码") from exc

    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None or "target" not in {
        name.strip() for name in reader.fieldnames if name
    }:
        raise ConfigurationError("CSV 必须包含 target 表头")
    items: list[ManifestItem] = []
    seen_bvids: set[str] = set()
    for row_number, row in enumerate(reader, start=2):
        normalized = {(key or "").strip(): value for key, value in row.items()}
        if not _enabled(normalized.get("enabled"), row_number):
            continue
        target = str(normalized.get("target") or "").strip()
        if not target:
            raise ConfigurationError(f"CSV 第 {row_number} 行缺少 target")
        try:
            bvid = parse_bvid(target)
        except BiliCommentsError as exc:
            raise ConfigurationError(f"CSV 第 {row_number} 行：{exc}") from exc
        comment_order = str(normalized.get("order") or "time").strip().lower()
        replies_mode = str(normalized.get("replies") or "root").strip().lower()
        if comment_order not in {"hot", "time"}:
            raise ConfigurationError(
                f"CSV 第 {row_number} 行 order 必须是 hot 或 time"
            )
        if replies_mode not in {"root", "all"}:
            raise ConfigurationError(
                f"CSV 第 {row_number} 行 replies 必须是 root 或 all"
            )
        if bvid in seen_bvids:
            raise ConfigurationError(f"CSV 第 {row_number} 行包含重复视频：{bvid}")
        seen_bvids.add(bvid)
        items.append(
            ManifestItem(
                row_number=row_number,
                target=target,
                bvid=bvid,
                comment_order=comment_order,
                replies_mode=replies_mode,
            )
        )
    if not items:
        raise ConfigurationError("CSV 中没有启用的目标")
    return Manifest(
        path=manifest_path,
        sha256=hashlib.sha256(raw).hexdigest(),
        items=tuple(items),
    )


def _summary_path(batch_id: int, requested: Path | None) -> Path:
    return requested.resolve() if requested else Path(f"data/batches/{batch_id}.json").resolve()


def _write_summary(database: Database, batch_id: int, path: Path) -> dict[str, object]:
    details = database.batch_details(batch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(details, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return details


def execute_batch(
    database: Database,
    source: SourceAdapter,
    batch_id: int,
    *,
    summary_path: Path | None = None,
) -> tuple[dict[str, object], int]:
    batch = database.get_batch_run(batch_id)
    existing_summary = str(batch["summary_path"] or "")
    output = _summary_path(
        batch_id,
        summary_path or (Path(existing_summary) if existing_summary else None),
    )
    database.set_batch_summary_path(batch_id, str(output))
    for item in list(database.iter_batch_items(batch_id, runnable_only=True)):
        item_id = int(item["id"])
        database.mark_batch_item_running(item_id)
        try:
            bvid, _ = crawl_target(
                str(item["target"]),
                source,
                database,
                comment_order=str(item["comment_order"]),
                replies_mode=str(item["replies_mode"]),
            )
            crawl_run_id = database.latest_crawl_run_id(
                bvid,
                source=source.name,
                comment_order=str(item["comment_order"]),
                replies_mode=str(item["replies_mode"]),
            )
            database.mark_batch_item_succeeded(item_id, crawl_run_id)
        except BiliCommentsError as exc:
            database.mark_batch_item_failed(item_id, str(exc))
    completed = database.finalize_batch(batch_id)
    details = _write_summary(database, batch_id, output)
    return details, 1 if int(completed["failed_count"]) else 0


def run_manifest(
    database: Database,
    source: SourceAdapter,
    manifest_path: str | Path,
    *,
    summary_path: Path | None = None,
) -> tuple[dict[str, object], int]:
    manifest = parse_manifest(manifest_path)
    batch = database.create_batch_run(
        str(manifest.path),
        manifest.sha256,
        (item.database_values() for item in manifest.items),
    )
    return execute_batch(
        database, source, int(batch["id"]), summary_path=summary_path
    )


def resume_batch(
    database: Database,
    source: SourceAdapter,
    batch_id: int,
    *,
    summary_path: Path | None = None,
) -> tuple[dict[str, object], int]:
    database.prepare_batch_resume(batch_id)
    return execute_batch(database, source, batch_id, summary_path=summary_path)
