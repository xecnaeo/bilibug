import sqlite3

from bili_comments.database import Database, SCHEMA_VERSION


def create_v1_database(path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE videos (
            bvid TEXT PRIMARY KEY,
            aid INTEGER NOT NULL UNIQUE,
            title TEXT NOT NULL,
            owner_mid TEXT NOT NULL,
            owner_name TEXT NOT NULL,
            source_url TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_crawled_at TEXT NOT NULL
        );
        CREATE TABLE crawl_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bvid TEXT NOT NULL REFERENCES videos(bvid),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL,
            next_cursor TEXT NOT NULL DEFAULT '',
            fetched_count INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        CREATE TABLE comments (
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
        INSERT INTO videos VALUES (
            'BV1xx411c7mD', 1, '旧标题', '2', '旧作者', 'BV1xx411c7mD',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        );
        INSERT INTO crawl_runs (
            bvid, started_at, completed_at, status, fetched_count
        ) VALUES (
            'BV1xx411c7mD', '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:01:00+00:00', 'completed', 1
        );
        INSERT INTO comments VALUES (
            'BV1xx411c7mD', 99, '旧评论', 1700000000, 3, 1, 7,
            '42', '旧用户', 4, '2026-01-01T00:00:00+00:00',
            '2026-01-01T00:00:00+00:00'
        );
        """
    )
    connection.close()


def test_v1_database_migrates_without_data_loss_and_is_idempotent(tmp_path) -> None:
    path = tmp_path / "v1.sqlite"
    create_v1_database(path)
    database = Database(path)
    assert database.connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    row = list(database.iter_comments("BV1xx411c7mD"))[0]
    assert row["message"] == "旧评论"
    assert row["sort_rank"] == 7
    assert database.get_run(1)["completeness"] == "complete"
    database.close()

    reopened = Database(path)
    assert len(list(reopened.iter_comments("BV1xx411c7mD"))) == 1
    reopened.close()


def test_stale_hot_cursor_starts_new_run(tmp_path) -> None:
    database = Database(tmp_path / "db.sqlite")
    database.connection.execute(
        """
        INSERT INTO videos (
            bvid, aid, title, owner_mid, owner_name, source_url,
            first_seen_at, last_crawled_at
        ) VALUES ('BV1xx411c7mD', 1, '', '', '', '', '2020-01-01', '2020-01-01')
        """
    )
    database.connection.commit()
    first = database.start_or_resume_run(
        "BV1xx411c7mD", source="test", comment_order="hot", replies_mode="root"
    )
    database.connection.execute(
        "UPDATE crawl_runs SET checkpoint_updated_at = '2020-01-01T00:00:00+00:00' WHERE id = ?",
        (first["id"],),
    )
    database.connection.commit()
    second = database.start_or_resume_run(
        "BV1xx411c7mD",
        source="test",
        comment_order="hot",
        replies_mode="root",
        cursor_ttl_seconds=1,
    )
    assert second["id"] != first["id"]
    assert database.get_run(first["id"])["end_reason"] == "stale_cursor"
    database.close()


def test_schema_does_not_store_extended_profile_fields(tmp_path) -> None:
    database = Database(tmp_path / "db.sqlite")
    columns = database._columns("comments")
    assert not {"avatar", "sex", "sign", "vip", "fans_badge"} & columns
    database.close()


def test_v2_database_migrates_to_batch_schema(tmp_path) -> None:
    path = tmp_path / "v2.sqlite"
    database = Database(path)
    database.connection.execute("DROP TABLE batch_items")
    database.connection.execute("DROP TABLE batch_runs")
    database.connection.execute("PRAGMA user_version = 2")
    database.connection.commit()
    database.close()

    migrated = Database(path)
    assert migrated.connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert migrated._table_exists("batch_runs")
    assert migrated._table_exists("batch_items")
    migrated.close()
