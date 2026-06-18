import csv
import json

from bili_comments.client import Video
from bili_comments.database import Database
from bili_comments.exporter import EXPORT_FIELDS, export_comments


def sample_reply() -> dict:
    return {
        "rpid": 99,
        "ctime": 1700000000,
        "like": 3,
        "rcount": 1,
        "content": {"message": "中文，\n带换行和\"引号\""},
        "member": {
            "mid": "123",
            "uname": "用户",
            "level_info": {"current_level": 4},
        },
    }


def prepared_database(path) -> Database:
    database = Database(path)
    video = Video(1, "BV1xx411c7mD", "标题", "2", "作者")
    database.upsert_video(video, video.bvid)
    run = database.start_or_resume_run(video.bvid)
    database.save_page(run["id"], video.bvid, [sample_reply()], rank_start=0, next_cursor=None)
    return database


def test_csv_export_handles_unicode_and_newlines(tmp_path) -> None:
    database = prepared_database(tmp_path / "db.sqlite")
    output = tmp_path / "comments.csv"
    assert export_comments(database, "BV1xx411c7mD", "csv", output) == 1
    assert output.read_bytes().startswith(b"\xef\xbb\xbf")
    with output.open(encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))
    assert tuple(rows[0]) == EXPORT_FIELDS
    assert rows[0]["message"] == sample_reply()["content"]["message"]
    database.close()


def test_jsonl_export_preserves_unicode(tmp_path) -> None:
    database = prepared_database(tmp_path / "db.sqlite")
    output = tmp_path / "comments.jsonl"
    export_comments(database, "BV1xx411c7mD", "jsonl", output)
    text = output.read_text(encoding="utf-8")
    assert "中文" in text
    assert json.loads(text)["rpid"] == 99
    database.close()


def test_comments_from_different_videos_are_isolated(tmp_path) -> None:
    database = Database(tmp_path / "db.sqlite")
    first = Video(1, "BV1xx411c7mD", "一", "2", "作者")
    second = Video(2, "BV1yy411c7mE", "二", "3", "作者")
    for video in (first, second):
        database.upsert_video(video, video.bvid)
        run = database.start_or_resume_run(video.bvid)
        database.save_page(
            run["id"], video.bvid, [sample_reply()], rank_start=0, next_cursor=None
        )
    assert len(list(database.iter_comments(first.bvid))) == 1
    assert len(list(database.iter_comments(second.bvid))) == 1
    database.close()
