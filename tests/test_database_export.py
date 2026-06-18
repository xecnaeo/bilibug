import csv
import json

from bili_comments.database import Database
from bili_comments.exporter import EXPORT_FIELDS, export_comments, export_records
from bili_comments.models import Comment, Video, VideoStats


def sample_comment() -> Comment:
    return Comment(
        rpid=99,
        message='中文，\n带换行和"引号"',
        ctime=1700000000,
        like_count=3,
        reply_count=1,
        author_mid="123",
        author_name="用户",
        author_level=4,
    )


def prepared_database(path) -> Database:
    database = Database(path)
    video = Video(
        1,
        "BV1xx411c7mD",
        "标题",
        "2",
        "作者",
        stats=VideoStats(view=10, like=2),
    )
    database.upsert_video(video, video.bvid)
    run = database.start_or_resume_run(
        video.bvid, source="test", comment_order="hot", replies_mode="root"
    )
    database.save_video_observation(run["id"], video)
    database.save_root_page(
        run["id"],
        video.bvid,
        [sample_comment()],
        rank_start=0,
        next_cursor=None,
        sort_order="hot",
        replies_mode="root",
    )
    return database


def test_csv_export_handles_unicode_and_newlines(tmp_path) -> None:
    database = prepared_database(tmp_path / "db.sqlite")
    output = tmp_path / "comments.csv"
    assert export_comments(database, "BV1xx411c7mD", "csv", output) == 1
    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    with output.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert tuple(rows[0]) == EXPORT_FIELDS
    assert rows[0]["message"] == sample_comment().message
    database.close()


def test_jsonl_and_observation_exports(tmp_path) -> None:
    database = prepared_database(tmp_path / "db.sqlite")
    comments = tmp_path / "comments.jsonl"
    stats = tmp_path / "stats.jsonl"
    export_records(database, "BV1xx411c7mD", "comments", "jsonl", comments)
    assert "中文" in comments.read_text(encoding="utf-8")
    assert json.loads(comments.read_text(encoding="utf-8"))["rpid"] == 99
    assert export_records(
        database, "BV1xx411c7mD", "video-stats", "jsonl", stats
    ) == 1
    assert json.loads(stats.read_text(encoding="utf-8"))["view_count"] == 10
    database.close()


def test_comments_from_different_videos_are_isolated(tmp_path) -> None:
    database = Database(tmp_path / "db.sqlite")
    videos = (
        Video(1, "BV1xx411c7mD", "一", "2", "作者"),
        Video(2, "BV1yy411c7mE", "二", "3", "作者"),
    )
    for video in videos:
        database.upsert_video(video, video.bvid)
        run = database.start_or_resume_run(
            video.bvid, source="test", comment_order="hot", replies_mode="root"
        )
        database.save_root_page(
            run["id"],
            video.bvid,
            [sample_comment()],
            rank_start=0,
            next_cursor=None,
            sort_order="hot",
            replies_mode="root",
        )
    assert len(list(database.iter_comments(videos[0].bvid))) == 1
    assert len(list(database.iter_comments(videos[1].bvid))) == 1
    database.close()
