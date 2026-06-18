from __future__ import annotations

import re

from .client import BiliClient
from .database import Database
from .errors import BiliCommentsError, InvalidTargetError

BVID_PATTERN = re.compile(r"(?<![A-Za-z0-9])(BV[A-Za-z0-9]{10})(?![A-Za-z0-9])")


def parse_bvid(target: str) -> str:
    match = BVID_PATTERN.search(target.strip())
    if not match:
        raise InvalidTargetError(f"无法从目标中识别 BV 号：{target}")
    return match.group(1)


def crawl_target(target: str, client: BiliClient, database: Database) -> tuple[str, int]:
    bvid = parse_bvid(target)
    video = client.get_video(bvid)
    database.upsert_video(video, target)
    run = database.start_or_resume_run(video.bvid)
    run_id = int(run["id"])
    cursor = str(run["next_cursor"])
    fetched_count = int(run["fetched_count"])
    try:
        while True:
            page = client.get_comment_page(video.aid, cursor)
            fetched_count = database.save_page(
                run_id,
                video.bvid,
                page.replies,
                rank_start=fetched_count,
                next_cursor=page.next_cursor,
            )
            if page.next_cursor is None:
                return video.bvid, fetched_count
            cursor = page.next_cursor
    except (BiliCommentsError, KeyError, TypeError, ValueError) as exc:
        database.fail_run(run_id, str(exc))
        if isinstance(exc, BiliCommentsError):
            raise
        raise BiliCommentsError(f"评论数据结构无效：{exc}") from exc
