from dataclasses import dataclass

import pytest

from bili_comments.client import CommentPage, Video
from bili_comments.database import Database
from bili_comments.errors import ApiError, InvalidTargetError
from bili_comments.service import crawl_target, parse_bvid


def reply(rpid: int, message: str, likes: int = 0) -> dict:
    return {
        "rpid": rpid,
        "ctime": 1700000000,
        "like": likes,
        "rcount": 2,
        "content": {"message": message},
        "member": {
            "mid": "42",
            "uname": "测试用户",
            "level_info": {"current_level": 5},
        },
    }


@dataclass
class FakeClient:
    pages: list[CommentPage]
    expected_cursors: list[str]

    def get_video(self, bvid: str) -> Video:
        return Video(123, bvid, "测试视频", "7", "UP主")

    def get_comment_page(self, aid: int, cursor: str = "") -> CommentPage:
        assert aid == 123
        assert cursor == self.expected_cursors.pop(0)
        return self.pages.pop(0)


def test_parse_bvid_from_id_and_url() -> None:
    bvid = "BV1xx411c7mD"
    assert parse_bvid(bvid) == bvid
    assert parse_bvid(f"https://www.bilibili.com/video/{bvid}?p=1") == bvid
    with pytest.raises(InvalidTargetError):
        parse_bvid("not-a-video")


def test_crawl_multiple_pages_and_recrawl_updates(tmp_path) -> None:
    database = Database(tmp_path / "comments.db")
    first_client = FakeClient(
        [
            CommentPage([reply(1, "第一条")], "next"),
            CommentPage([reply(2, "第二条")], None),
        ],
        ["", "next"],
    )
    bvid, count = crawl_target("BV1xx411c7mD", first_client, database)  # type: ignore[arg-type]
    assert count == 2
    assert [row["rpid"] for row in database.iter_comments(bvid)] == [1, 2]

    second_client = FakeClient([CommentPage([reply(1, "已更新", 9)], None)], [""])
    _, count = crawl_target(bvid, second_client, database)  # type: ignore[arg-type]
    rows = list(database.iter_comments(bvid))
    assert count == 1
    assert len(rows) == 2
    assert rows[0]["message"] == "已更新"
    assert rows[0]["like_count"] == 9
    database.close()


class FailingClient(FakeClient):
    def get_comment_page(self, aid: int, cursor: str = "") -> CommentPage:
        if cursor == "next":
            raise ApiError("temporary failure")
        return super().get_comment_page(aid, cursor)


def test_failed_run_resumes_from_saved_cursor(tmp_path) -> None:
    database = Database(tmp_path / "comments.db")
    failing = FailingClient(
        [CommentPage([reply(1, "已保存")], "next")],
        [""],
    )
    with pytest.raises(ApiError):
        crawl_target("BV1xx411c7mD", failing, database)  # type: ignore[arg-type]

    resumed = FakeClient([CommentPage([reply(2, "续抓")], None)], ["next"])
    _, count = crawl_target("BV1xx411c7mD", resumed, database)  # type: ignore[arg-type]
    assert count == 2
    assert [row["rpid"] for row in database.iter_comments("BV1xx411c7mD")] == [1, 2]
    database.close()
