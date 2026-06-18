from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .client import Video


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection:
            yield self.connection

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS videos (
                bvid TEXT PRIMARY KEY,
                aid INTEGER NOT NULL UNIQUE,
                title TEXT NOT NULL,
                owner_mid TEXT NOT NULL,
                owner_name TEXT NOT NULL,
                source_url TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_crawled_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS crawl_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bvid TEXT NOT NULL REFERENCES videos(bvid),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
                next_cursor TEXT NOT NULL DEFAULT '',
                fetched_count INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS comments (
                bvid TEXT NOT NULL REFERENCES videos(bvid),
                rpid INTEGER NOT NULL,
                message TEXT NOT NULL,
                ctime INTEGER NOT NULL,
                like_count INTEGER NOT NULL,
                reply_count INTEGER NOT NULL,
                hot_rank INTEGER NOT NULL,
                author_mid TEXT NOT NULL,
                author_name TEXT NOT NULL,
                author_level INTEGER,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (bvid, rpid)
            );

            CREATE INDEX IF NOT EXISTS comments_bvid_rank
            ON comments (bvid, hot_rank, rpid);
            """
        )

    def upsert_video(self, video: Video, source_url: str) -> None:
        now = utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO videos (
                    bvid, aid, title, owner_mid, owner_name, source_url,
                    first_seen_at, last_crawled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                    aid = excluded.aid,
                    title = excluded.title,
                    owner_mid = excluded.owner_mid,
                    owner_name = excluded.owner_name,
                    source_url = excluded.source_url,
                    last_crawled_at = excluded.last_crawled_at
                """,
                (
                    video.bvid, video.aid, video.title, video.owner_mid,
                    video.owner_name, source_url, now, now,
                ),
            )

    def start_or_resume_run(self, bvid: str) -> sqlite3.Row:
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM crawl_runs
                WHERE bvid = ? AND status IN ('running', 'failed')
                ORDER BY id DESC LIMIT 1
                """,
                (bvid,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE crawl_runs SET status = 'running', error = NULL WHERE id = ?",
                    (row["id"],),
                )
                return connection.execute(
                    "SELECT * FROM crawl_runs WHERE id = ?", (row["id"],)
                ).fetchone()
            cursor = connection.execute(
                """
                INSERT INTO crawl_runs (bvid, started_at, status)
                VALUES (?, ?, 'running')
                """,
                (bvid, utc_now()),
            )
            return connection.execute(
                "SELECT * FROM crawl_runs WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()

    @staticmethod
    def _comment_values(
        bvid: str, reply: dict[str, Any], hot_rank: int, now: str
    ) -> tuple[object, ...]:
        member = reply.get("member") if isinstance(reply.get("member"), dict) else {}
        content = reply.get("content") if isinstance(reply.get("content"), dict) else {}
        level_info = (
            member.get("level_info") if isinstance(member.get("level_info"), dict) else {}
        )
        return (
            bvid,
            int(reply["rpid"]),
            str(content.get("message") or ""),
            int(reply.get("ctime") or 0),
            int(reply.get("like") or 0),
            int(reply.get("rcount", reply.get("count", 0)) or 0),
            hot_rank,
            str(member.get("mid") or ""),
            str(member.get("uname") or ""),
            int(level_info["current_level"]) if level_info.get("current_level") is not None else None,
            now,
            now,
        )

    def save_page(
        self,
        run_id: int,
        bvid: str,
        replies: Iterable[dict[str, Any]],
        *,
        rank_start: int,
        next_cursor: str | None,
    ) -> int:
        now = utc_now()
        values = [
            self._comment_values(bvid, reply, rank_start + index, now)
            for index, reply in enumerate(replies)
        ]
        with self._transaction() as connection:
            connection.executemany(
                """
                INSERT INTO comments (
                    bvid, rpid, message, ctime, like_count, reply_count,
                    hot_rank, author_mid, author_name, author_level,
                    first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid, rpid) DO UPDATE SET
                    message = excluded.message,
                    ctime = excluded.ctime,
                    like_count = excluded.like_count,
                    reply_count = excluded.reply_count,
                    hot_rank = excluded.hot_rank,
                    author_mid = excluded.author_mid,
                    author_name = excluded.author_name,
                    author_level = excluded.author_level,
                    last_seen_at = excluded.last_seen_at
                """,
                values,
            )
            fetched_count = rank_start + len(values)
            if next_cursor is None:
                connection.execute(
                    """
                    UPDATE crawl_runs
                    SET status = 'completed', next_cursor = '', fetched_count = ?,
                        completed_at = ?, error = NULL
                    WHERE id = ?
                    """,
                    (fetched_count, now, run_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE crawl_runs
                    SET next_cursor = ?, fetched_count = ?, error = NULL
                    WHERE id = ?
                    """,
                    (next_cursor, fetched_count, run_id),
                )
        return fetched_count

    def fail_run(self, run_id: int, error: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "UPDATE crawl_runs SET status = 'failed', error = ? WHERE id = ?",
                (error, run_id),
            )

    def iter_comments(self, bvid: str) -> Iterator[sqlite3.Row]:
        yield from self.connection.execute(
            """
            SELECT rpid, bvid, message, ctime, like_count, reply_count,
                   hot_rank, author_mid, author_name, author_level,
                   first_seen_at, last_seen_at
            FROM comments WHERE bvid = ?
            ORDER BY hot_rank, rpid
            """,
            (bvid,),
        )

    def has_video(self, bvid: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM videos WHERE bvid = ?", (bvid,)
        ).fetchone() is not None
