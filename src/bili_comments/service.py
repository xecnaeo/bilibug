from __future__ import annotations

import re

from .database import Database
from .errors import BiliCommentsError, InvalidTargetError
from .source import SourceAdapter

BVID_PATTERN = re.compile(r"(?<![A-Za-z0-9])(BV[A-Za-z0-9]{10})(?![A-Za-z0-9])")


def parse_bvid(target: str) -> str:
    match = BVID_PATTERN.search(target.strip())
    if not match:
        raise InvalidTargetError(f"无法从目标中识别 BV 号：{target}")
    return match.group(1)


def _crawl_root_comments(
    source: SourceAdapter,
    database: Database,
    *,
    run_id: int,
    aid: int,
    bvid: str,
    comment_order: str,
    replies_mode: str,
) -> None:
    run = database.get_run(run_id)
    cursor = str(run["next_cursor"])
    rank_start = int(run["fetched_count"])
    while not bool(run["root_finished"]):
        page = source.get_comment_page(aid, cursor, order=comment_order)
        rank_start = database.save_root_page(
            run_id,
            bvid,
            page.comments,
            rank_start=rank_start,
            next_cursor=page.next_cursor,
            sort_order=comment_order,
            replies_mode=replies_mode,
        )
        if page.next_cursor is None:
            return
        cursor = page.next_cursor
        run = database.get_run(run_id)


def _crawl_sub_replies(
    source: SourceAdapter,
    database: Database,
    *,
    run_id: int,
    aid: int,
    bvid: str,
) -> None:
    roots = database.roots_for_run(run_id)
    if not roots:
        database.complete_all_replies_run(run_id)
        return
    run = database.get_run(run_id)
    checkpoint_root = run["sub_root_rpid"]
    if checkpoint_root is not None and int(checkpoint_root) in roots:
        start_index = roots.index(int(checkpoint_root))
        first_page = int(run["sub_page"])
    else:
        start_index = 0
        first_page = 1

    for index in range(start_index, len(roots)):
        root_rpid = roots[index]
        page_number = first_page if index == start_index else 1
        while True:
            page = source.get_sub_reply_page(aid, root_rpid, page_number)
            next_root = (
                roots[index + 1]
                if page.next_page is None and index + 1 < len(roots)
                else None
            )
            database.save_sub_reply_page(
                run_id,
                bvid,
                page.comments,
                next_page=page.next_page,
                current_root_rpid=root_rpid,
                next_root_rpid=next_root,
            )
            if page.next_page is None:
                break
            page_number = page.next_page


def crawl_target(
    target: str,
    source: SourceAdapter,
    database: Database,
    *,
    comment_order: str = "hot",
    replies_mode: str = "root",
    cursor_ttl_seconds: int | None = None,
) -> tuple[str, int]:
    bvid = parse_bvid(target)
    video = source.get_video(bvid)
    database.upsert_video(video, target)
    if cursor_ttl_seconds is None:
        run = database.start_or_resume_run(
            video.bvid,
            source=source.name,
            comment_order=comment_order,
            replies_mode=replies_mode,
        )
    else:
        run = database.start_or_resume_run(
            video.bvid,
            source=source.name,
            comment_order=comment_order,
            replies_mode=replies_mode,
            cursor_ttl_seconds=cursor_ttl_seconds,
        )
    run_id = int(run["id"])
    database.save_video_observation(run_id, video)
    try:
        if not bool(run["root_finished"]):
            _crawl_root_comments(
                source,
                database,
                run_id=run_id,
                aid=video.aid,
                bvid=video.bvid,
                comment_order=comment_order,
                replies_mode=replies_mode,
            )
        if replies_mode == "all" and database.get_run(run_id)["status"] != "completed":
            _crawl_sub_replies(
                source,
                database,
                run_id=run_id,
                aid=video.aid,
                bvid=video.bvid,
            )
        completed = database.get_run(run_id)
        return video.bvid, int(completed["fetched_count"])
    except (BiliCommentsError, KeyError, TypeError, ValueError) as exc:
        database.fail_run(run_id, str(exc))
        if isinstance(exc, BiliCommentsError):
            raise
        raise BiliCommentsError(f"评论数据结构无效：{exc}") from exc
