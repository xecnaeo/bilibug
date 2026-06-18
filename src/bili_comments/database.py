from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import BiliCommentsError
from .models import Comment, Video

SCHEMA_VERSION = 2
HOT_CURSOR_TTL_SECONDS = 6 * 60 * 60


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._initialize_schema()

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

    def _table_exists(self, name: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None

    def _columns(self, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in self.connection.execute(f"PRAGMA table_info({table})")
        }

    def _initialize_schema(self) -> None:
        version = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise BiliCommentsError(
                f"数据库版本 {version} 高于当前程序支持的 {SCHEMA_VERSION}"
            )
        if not self._table_exists("videos"):
            self._create_v2_schema()
            return
        if version < SCHEMA_VERSION:
            self._migrate_v1_to_v2()
        else:
            with self._transaction() as connection:
                self._create_v2_additions(connection)

    def _create_v2_schema(self) -> None:
        with self._transaction() as connection:
            connection.executescript(
                """
                CREATE TABLE videos (
                    bvid TEXT PRIMARY KEY,
                    aid INTEGER NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    owner_mid TEXT NOT NULL,
                    owner_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    cover_url TEXT NOT NULL DEFAULT '',
                    category_id INTEGER NOT NULL DEFAULT 0,
                    category_name TEXT NOT NULL DEFAULT '',
                    published_at INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL DEFAULT 0,
                    duration INTEGER NOT NULL DEFAULT 0,
                    copyright INTEGER NOT NULL DEFAULT 0,
                    state INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    last_crawled_at TEXT NOT NULL
                );

                CREATE TABLE crawl_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    bvid TEXT NOT NULL REFERENCES videos(bvid),
                    source TEXT NOT NULL DEFAULT 'bilibili-web',
                    comment_order TEXT NOT NULL DEFAULT 'hot',
                    replies_mode TEXT NOT NULL DEFAULT 'root',
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
                    next_cursor TEXT NOT NULL DEFAULT '',
                    checkpoint_updated_at TEXT NOT NULL,
                    root_finished INTEGER NOT NULL DEFAULT 0,
                    sub_root_rpid INTEGER,
                    sub_page INTEGER NOT NULL DEFAULT 1,
                    fetched_count INTEGER NOT NULL DEFAULT 0,
                    completeness TEXT NOT NULL DEFAULT 'partial',
                    end_reason TEXT,
                    error TEXT
                );

                CREATE TABLE comments (
                    bvid TEXT NOT NULL REFERENCES videos(bvid),
                    rpid INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    ctime INTEGER NOT NULL,
                    like_count INTEGER NOT NULL,
                    reply_count INTEGER NOT NULL,
                    hot_rank INTEGER NOT NULL DEFAULT 0,
                    sort_order TEXT NOT NULL DEFAULT 'hot',
                    sort_rank INTEGER NOT NULL DEFAULT 0,
                    root_rpid INTEGER NOT NULL DEFAULT 0,
                    parent_rpid INTEGER NOT NULL DEFAULT 0,
                    level INTEGER NOT NULL DEFAULT 0,
                    pin_type TEXT NOT NULL DEFAULT '',
                    state INTEGER NOT NULL DEFAULT 0,
                    author_mid TEXT NOT NULL,
                    author_name TEXT NOT NULL,
                    author_level INTEGER,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (bvid, rpid)
                );
                """
            )
            self._create_v2_additions(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _create_v2_additions(
        self, connection: sqlite3.Connection | None = None
    ) -> None:
        target = connection or self.connection
        statements = (
            """
            CREATE TABLE IF NOT EXISTS video_pages (
                bvid TEXT NOT NULL REFERENCES videos(bvid),
                cid INTEGER NOT NULL,
                page_number INTEGER NOT NULL,
                title TEXT NOT NULL,
                duration INTEGER NOT NULL,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                rotate INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (bvid, cid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS video_observations (
                run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
                bvid TEXT NOT NULL REFERENCES videos(bvid),
                view_count INTEGER NOT NULL,
                danmaku_count INTEGER NOT NULL,
                reply_count INTEGER NOT NULL,
                favorite_count INTEGER NOT NULL,
                coin_count INTEGER NOT NULL,
                share_count INTEGER NOT NULL,
                like_count INTEGER NOT NULL,
                current_rank INTEGER NOT NULL,
                historical_rank INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (run_id, bvid)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS comment_observations (
                run_id INTEGER NOT NULL REFERENCES crawl_runs(id) ON DELETE CASCADE,
                bvid TEXT NOT NULL,
                rpid INTEGER NOT NULL,
                like_count INTEGER NOT NULL,
                reply_count INTEGER NOT NULL,
                sort_order TEXT NOT NULL,
                sort_rank INTEGER NOT NULL,
                state INTEGER NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (run_id, bvid, rpid),
                FOREIGN KEY (bvid, rpid) REFERENCES comments(bvid, rpid)
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS comments_bvid_rank
            ON comments (bvid, level, sort_rank, rpid)
            """,
            """
            CREATE INDEX IF NOT EXISTS comments_bvid_root
            ON comments (bvid, root_rpid, parent_rpid)
            """,
            """
            CREATE INDEX IF NOT EXISTS observations_bvid_rpid
            ON comment_observations (bvid, rpid, observed_at)
            """,
        )
        for statement in statements:
            target.execute(statement)

    def _add_column(self, table: str, definition: str) -> None:
        name = definition.split()[0]
        if name not in self._columns(table):
            self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _migrate_v1_to_v2(self) -> None:
        with self._transaction() as connection:
            for definition in (
                "description TEXT NOT NULL DEFAULT ''",
                "cover_url TEXT NOT NULL DEFAULT ''",
                "category_id INTEGER NOT NULL DEFAULT 0",
                "category_name TEXT NOT NULL DEFAULT ''",
                "published_at INTEGER NOT NULL DEFAULT 0",
                "created_at INTEGER NOT NULL DEFAULT 0",
                "duration INTEGER NOT NULL DEFAULT 0",
                "copyright INTEGER NOT NULL DEFAULT 0",
                "state INTEGER NOT NULL DEFAULT 0",
            ):
                self._add_column("videos", definition)
            for definition in (
                "source TEXT NOT NULL DEFAULT 'bilibili-web'",
                "comment_order TEXT NOT NULL DEFAULT 'hot'",
                "replies_mode TEXT NOT NULL DEFAULT 'root'",
                "checkpoint_updated_at TEXT",
                "root_finished INTEGER NOT NULL DEFAULT 0",
                "sub_root_rpid INTEGER",
                "sub_page INTEGER NOT NULL DEFAULT 1",
                "completeness TEXT NOT NULL DEFAULT 'partial'",
                "end_reason TEXT",
            ):
                self._add_column("crawl_runs", definition)
            for definition in (
                "sort_order TEXT NOT NULL DEFAULT 'hot'",
                "sort_rank INTEGER NOT NULL DEFAULT 0",
                "root_rpid INTEGER NOT NULL DEFAULT 0",
                "parent_rpid INTEGER NOT NULL DEFAULT 0",
                "level INTEGER NOT NULL DEFAULT 0",
                "pin_type TEXT NOT NULL DEFAULT ''",
                "state INTEGER NOT NULL DEFAULT 0",
            ):
                self._add_column("comments", definition)
            connection.execute(
                "UPDATE comments SET sort_rank = hot_rank WHERE sort_rank = 0"
            )
            connection.execute(
                """
                UPDATE crawl_runs
                SET checkpoint_updated_at = COALESCE(checkpoint_updated_at, completed_at, started_at),
                    root_finished = CASE WHEN status = 'completed' THEN 1 ELSE root_finished END,
                    completeness = CASE WHEN status = 'completed' THEN 'complete' ELSE completeness END,
                    end_reason = CASE WHEN status = 'completed' THEN 'exhausted' ELSE end_reason END
                """
            )
            connection.execute("DROP INDEX IF EXISTS comments_bvid_rank")
            self._create_v2_additions(connection)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def upsert_video(self, video: Video, source_url: str) -> None:
        now = utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO videos (
                    bvid, aid, title, owner_mid, owner_name, source_url,
                    description, cover_url, category_id, category_name,
                    published_at, created_at, duration, copyright, state,
                    first_seen_at, last_crawled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid) DO UPDATE SET
                    aid = excluded.aid,
                    title = excluded.title,
                    owner_mid = excluded.owner_mid,
                    owner_name = excluded.owner_name,
                    source_url = excluded.source_url,
                    description = excluded.description,
                    cover_url = excluded.cover_url,
                    category_id = excluded.category_id,
                    category_name = excluded.category_name,
                    published_at = excluded.published_at,
                    created_at = excluded.created_at,
                    duration = excluded.duration,
                    copyright = excluded.copyright,
                    state = excluded.state,
                    last_crawled_at = excluded.last_crawled_at
                """,
                (
                    video.bvid,
                    video.aid,
                    video.title,
                    video.owner_mid,
                    video.owner_name,
                    source_url,
                    video.description,
                    video.cover_url,
                    video.category_id,
                    video.category_name,
                    video.published_at,
                    video.created_at,
                    video.duration,
                    video.copyright,
                    video.state,
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO video_pages (
                    bvid, cid, page_number, title, duration, width, height, rotate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bvid, cid) DO UPDATE SET
                    page_number = excluded.page_number,
                    title = excluded.title,
                    duration = excluded.duration,
                    width = excluded.width,
                    height = excluded.height,
                    rotate = excluded.rotate
                """,
                (
                    (
                        video.bvid,
                        page.cid,
                        page.page,
                        page.title,
                        page.duration,
                        page.width,
                        page.height,
                        page.rotate,
                    )
                    for page in video.pages
                ),
            )

    @staticmethod
    def _is_stale(row: sqlite3.Row, ttl_seconds: int) -> bool:
        value = row["checkpoint_updated_at"] or row["started_at"]
        try:
            updated = datetime.fromisoformat(str(value))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - updated).total_seconds() > ttl_seconds
        except ValueError:
            return True

    def start_or_resume_run(
        self,
        bvid: str,
        *,
        source: str,
        comment_order: str,
        replies_mode: str,
        cursor_ttl_seconds: int = HOT_CURSOR_TTL_SECONDS,
    ) -> sqlite3.Row:
        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM crawl_runs
                WHERE bvid = ? AND source = ? AND comment_order = ?
                  AND replies_mode = ? AND status IN ('running', 'failed')
                ORDER BY id DESC LIMIT 1
                """,
                (bvid, source, comment_order, replies_mode),
            ).fetchone()
            if row is not None:
                stale_hot_cursor = (
                    comment_order == "hot"
                    and not bool(row["root_finished"])
                    and self._is_stale(row, cursor_ttl_seconds)
                )
                if stale_hot_cursor:
                    connection.execute(
                        """
                        UPDATE crawl_runs
                        SET status = 'failed', completeness = 'partial',
                            end_reason = 'stale_cursor', error = '热门游标已过期，重新抓取'
                        WHERE id = ?
                        """,
                        (row["id"],),
                    )
                    row = None
            if row is not None:
                connection.execute(
                    """
                    UPDATE crawl_runs
                    SET status = 'running', error = NULL, end_reason = NULL
                    WHERE id = ?
                    """,
                    (row["id"],),
                )
                return self.get_run(int(row["id"]))
            cursor = connection.execute(
                """
                INSERT INTO crawl_runs (
                    bvid, source, comment_order, replies_mode, started_at,
                    status, checkpoint_updated_at
                ) VALUES (?, ?, ?, ?, ?, 'running', ?)
                """,
                (bvid, source, comment_order, replies_mode, now, now),
            )
            return self.get_run(int(cursor.lastrowid))

    def get_run(self, run_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM crawl_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise BiliCommentsError(f"抓取运行不存在：{run_id}")
        return row

    def save_video_observation(self, run_id: int, video: Video) -> None:
        stats = video.stats
        with self._transaction() as connection:
            connection.execute(
                """
                INSERT INTO video_observations (
                    run_id, bvid, view_count, danmaku_count, reply_count,
                    favorite_count, coin_count, share_count, like_count,
                    current_rank, historical_rank, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, bvid) DO UPDATE SET
                    view_count = excluded.view_count,
                    danmaku_count = excluded.danmaku_count,
                    reply_count = excluded.reply_count,
                    favorite_count = excluded.favorite_count,
                    coin_count = excluded.coin_count,
                    share_count = excluded.share_count,
                    like_count = excluded.like_count,
                    current_rank = excluded.current_rank,
                    historical_rank = excluded.historical_rank,
                    observed_at = excluded.observed_at
                """,
                (
                    run_id,
                    video.bvid,
                    stats.view,
                    stats.danmaku,
                    stats.reply,
                    stats.favorite,
                    stats.coin,
                    stats.share,
                    stats.like,
                    stats.current_rank,
                    stats.historical_rank,
                    utc_now(),
                ),
            )

    @staticmethod
    def _comment_values(
        bvid: str,
        comment: Comment,
        *,
        sort_order: str,
        sort_rank: int,
        now: str,
    ) -> tuple[object, ...]:
        hot_rank = sort_rank if sort_order == "hot" and comment.level == 0 else 0
        return (
            bvid,
            comment.rpid,
            comment.message,
            comment.ctime,
            comment.like_count,
            comment.reply_count,
            hot_rank,
            sort_order,
            sort_rank,
            comment.root_rpid,
            comment.parent_rpid,
            comment.level,
            comment.pin_type,
            comment.state,
            comment.author_mid,
            comment.author_name,
            comment.author_level,
            now,
            now,
        )

    def _save_comments(
        self,
        connection: sqlite3.Connection,
        run_id: int,
        bvid: str,
        comments: Iterable[Comment],
        *,
        sort_order: str,
        rank_offset: int,
        now: str,
    ) -> int:
        items = list(comments)
        values = [
            self._comment_values(
                bvid,
                comment,
                sort_order=sort_order,
                sort_rank=rank_offset + comment.sort_rank,
                now=now,
            )
            for comment in items
        ]
        connection.executemany(
            """
            INSERT INTO comments (
                bvid, rpid, message, ctime, like_count, reply_count,
                hot_rank, sort_order, sort_rank, root_rpid, parent_rpid,
                level, pin_type, state, author_mid, author_name, author_level,
                first_seen_at, last_seen_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bvid, rpid) DO UPDATE SET
                message = excluded.message,
                ctime = excluded.ctime,
                like_count = excluded.like_count,
                reply_count = excluded.reply_count,
                hot_rank = CASE WHEN excluded.sort_order = 'hot'
                                THEN excluded.hot_rank ELSE comments.hot_rank END,
                sort_order = excluded.sort_order,
                sort_rank = excluded.sort_rank,
                root_rpid = excluded.root_rpid,
                parent_rpid = excluded.parent_rpid,
                level = excluded.level,
                pin_type = excluded.pin_type,
                state = excluded.state,
                author_mid = excluded.author_mid,
                author_name = excluded.author_name,
                author_level = excluded.author_level,
                last_seen_at = excluded.last_seen_at
            """,
            values,
        )
        connection.executemany(
            """
            INSERT INTO comment_observations (
                run_id, bvid, rpid, like_count, reply_count, sort_order,
                sort_rank, state, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, bvid, rpid) DO UPDATE SET
                like_count = excluded.like_count,
                reply_count = excluded.reply_count,
                sort_order = excluded.sort_order,
                sort_rank = excluded.sort_rank,
                state = excluded.state,
                observed_at = excluded.observed_at
            """,
            (
                (
                    run_id,
                    bvid,
                    comment.rpid,
                    comment.like_count,
                    comment.reply_count,
                    sort_order,
                    rank_offset + comment.sort_rank,
                    comment.state,
                    now,
                )
                for comment in items
            ),
        )
        return len(items)

    def save_root_page(
        self,
        run_id: int,
        bvid: str,
        comments: Iterable[Comment],
        *,
        rank_start: int,
        next_cursor: str | None,
        sort_order: str,
        replies_mode: str,
    ) -> int:
        now = utc_now()
        with self._transaction() as connection:
            saved = self._save_comments(
                connection,
                run_id,
                bvid,
                comments,
                sort_order=sort_order,
                rank_offset=rank_start,
                now=now,
            )
            fetched_count = rank_start + saved
            root_finished = next_cursor is None
            completed = root_finished and replies_mode == "root"
            connection.execute(
                """
                UPDATE crawl_runs
                SET next_cursor = ?, fetched_count = ?, checkpoint_updated_at = ?,
                    root_finished = ?, status = ?, completed_at = ?,
                    completeness = ?, end_reason = ?, error = NULL
                WHERE id = ?
                """,
                (
                    next_cursor or "",
                    fetched_count,
                    now,
                    int(root_finished),
                    "completed" if completed else "running",
                    now if completed else None,
                    "complete" if completed else "partial",
                    "exhausted" if completed else None,
                    run_id,
                ),
            )
        return fetched_count

    def roots_for_run(self, run_id: int) -> list[int]:
        return [
            int(row["rpid"])
            for row in self.connection.execute(
                """
                SELECT c.rpid
                FROM comment_observations o
                JOIN comments c ON c.bvid = o.bvid AND c.rpid = o.rpid
                WHERE o.run_id = ? AND c.level = 0 AND o.reply_count > 0
                ORDER BY o.sort_rank, c.rpid
                """,
                (run_id,),
            )
        ]

    def complete_all_replies_run(self, run_id: int) -> None:
        now = utc_now()
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE crawl_runs
                SET status = 'completed', completed_at = ?, completeness = 'complete',
                    end_reason = 'exhausted', checkpoint_updated_at = ?, error = NULL
                WHERE id = ?
                """,
                (now, now, run_id),
            )

    def save_sub_reply_page(
        self,
        run_id: int,
        bvid: str,
        comments: Iterable[Comment],
        *,
        next_page: int | None,
        current_root_rpid: int,
        next_root_rpid: int | None,
    ) -> int:
        now = utc_now()
        with self._transaction() as connection:
            saved = self._save_comments(
                connection,
                run_id,
                bvid,
                comments,
                sort_order="thread",
                rank_offset=0,
                now=now,
            )
            if next_page is not None:
                sub_root_rpid = current_root_rpid
                sub_page = next_page
                completed = False
            elif next_root_rpid is not None:
                sub_root_rpid = next_root_rpid
                sub_page = 1
                completed = False
            else:
                sub_root_rpid = None
                sub_page = 1
                completed = True
            connection.execute(
                """
                UPDATE crawl_runs
                SET sub_root_rpid = ?, sub_page = ?,
                    fetched_count = fetched_count + ?, checkpoint_updated_at = ?,
                    status = ?, completed_at = ?, completeness = ?,
                    end_reason = ?, error = NULL
                WHERE id = ?
                """,
                (
                    sub_root_rpid,
                    sub_page,
                    saved,
                    now,
                    "completed" if completed else "running",
                    now if completed else None,
                    "complete" if completed else "partial",
                    "exhausted" if completed else None,
                    run_id,
                ),
            )
        return saved

    def fail_run(self, run_id: int, error: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                """
                UPDATE crawl_runs
                SET status = 'failed', completeness = 'partial',
                    end_reason = 'error', error = ?
                WHERE id = ?
                """,
                (error, run_id),
            )

    def iter_comments(self, bvid: str) -> Iterator[sqlite3.Row]:
        yield from self.connection.execute(
            """
            SELECT c.rpid, c.bvid, c.message, c.ctime, c.like_count,
                   c.reply_count, c.hot_rank, c.sort_order, c.sort_rank,
                   c.root_rpid, c.parent_rpid, c.level, c.pin_type, c.state,
                   c.author_mid, c.author_name, c.author_level,
                   c.first_seen_at, c.last_seen_at
            FROM comments c
            LEFT JOIN comments root
              ON root.bvid = c.bvid AND root.rpid = c.root_rpid
            WHERE c.bvid = ?
            ORDER BY COALESCE(root.sort_rank, c.sort_rank), c.level,
                     c.sort_rank, c.rpid
            """,
            (bvid,),
        )

    def iter_video_observations(self, bvid: str) -> Iterator[sqlite3.Row]:
        yield from self.connection.execute(
            """
            SELECT run_id, bvid, view_count, danmaku_count, reply_count,
                   favorite_count, coin_count, share_count, like_count,
                   current_rank, historical_rank, observed_at
            FROM video_observations WHERE bvid = ?
            ORDER BY observed_at, run_id
            """,
            (bvid,),
        )

    def iter_comment_observations(self, bvid: str) -> Iterator[sqlite3.Row]:
        yield from self.connection.execute(
            """
            SELECT run_id, bvid, rpid, like_count, reply_count, sort_order,
                   sort_rank, state, observed_at
            FROM comment_observations WHERE bvid = ?
            ORDER BY observed_at, run_id, sort_rank, rpid
            """,
            (bvid,),
        )

    def inspect_video(self, bvid: str) -> dict[str, object] | None:
        video = self.connection.execute(
            "SELECT * FROM videos WHERE bvid = ?", (bvid,)
        ).fetchone()
        if video is None:
            return None
        latest_run = self.connection.execute(
            "SELECT * FROM crawl_runs WHERE bvid = ? ORDER BY id DESC LIMIT 1",
            (bvid,),
        ).fetchone()
        counts = self.connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN level = 0 THEN 1 ELSE 0 END) AS roots,
                   SUM(CASE WHEN level > 0 THEN 1 ELSE 0 END) AS replies
            FROM comments WHERE bvid = ?
            """,
            (bvid,),
        ).fetchone()
        return {
            "video": dict(video),
            "comment_counts": dict(counts),
            "latest_run": dict(latest_run) if latest_run is not None else None,
            "schema_version": SCHEMA_VERSION,
        }

    def has_video(self, bvid: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM videos WHERE bvid = ?", (bvid,)
        ).fetchone() is not None
