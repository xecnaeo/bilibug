from dataclasses import dataclass, field

import pytest

from bili_comments.database import Database
from bili_comments.errors import ApiError, InvalidTargetError
from bili_comments.models import Comment, CommentPage, SubReplyPage, Video, VideoStats
from bili_comments.service import crawl_target, parse_bvid


def comment(
    rpid: int,
    message: str,
    *,
    likes: int = 0,
    replies: int = 0,
    root: int = 0,
    rank: int = 0,
) -> Comment:
    return Comment(
        rpid=rpid,
        message=message,
        ctime=1700000000,
        like_count=likes,
        reply_count=replies,
        author_mid="42",
        author_name="测试用户",
        author_level=5,
        root_rpid=root,
        parent_rpid=root,
        level=1 if root else 0,
        sort_rank=rank,
    )


@dataclass
class FakeSource:
    pages: list[CommentPage]
    expected_cursors: list[str]
    subpages: dict[tuple[int, int], SubReplyPage] = field(default_factory=dict)
    name: str = "fake"

    def get_video(self, bvid: str) -> Video:
        return Video(
            123,
            bvid,
            "测试视频",
            "7",
            "UP主",
            stats=VideoStats(view=100, reply=2, like=10),
        )

    def get_comment_page(
        self, aid: int, cursor: str = "", *, order: str = "hot"
    ) -> CommentPage:
        assert aid == 123
        assert cursor == self.expected_cursors.pop(0)
        return self.pages.pop(0)

    def get_sub_reply_page(
        self, aid: int, root_rpid: int, page: int
    ) -> SubReplyPage:
        return self.subpages[(root_rpid, page)]


def test_parse_bvid_from_id_and_url() -> None:
    bvid = "BV1xx411c7mD"
    assert parse_bvid(bvid) == bvid
    assert parse_bvid(f"https://www.bilibili.com/video/{bvid}?p=1") == bvid
    with pytest.raises(InvalidTargetError):
        parse_bvid("not-a-video")


def test_crawl_multiple_pages_and_recrawl_creates_observations(tmp_path) -> None:
    database = Database(tmp_path / "comments.db")
    first = FakeSource(
        [
            CommentPage((comment(1, "第一条", rank=0),), "next"),
            CommentPage((comment(2, "第二条", rank=0),), None),
        ],
        ["", "next"],
    )
    bvid, count = crawl_target("BV1xx411c7mD", first, database)
    assert count == 2

    second = FakeSource(
        [CommentPage((comment(1, "已更新", likes=9),), None)], [""]
    )
    _, count = crawl_target(bvid, second, database)
    rows = list(database.iter_comments(bvid))
    observations = list(database.iter_comment_observations(bvid))
    assert count == 1
    assert len(rows) == 2
    assert rows[0]["message"] == "已更新"
    assert rows[0]["like_count"] == 9
    assert len([row for row in observations if row["rpid"] == 1]) == 2
    assert len(list(database.iter_video_observations(bvid))) == 2
    database.close()


def test_all_replies_crawls_full_thread_and_relationships(tmp_path) -> None:
    database = Database(tmp_path / "comments.db")
    source = FakeSource(
        [CommentPage((comment(1, "根", replies=2),), None)],
        [""],
        {
            (1, 1): SubReplyPage((comment(2, "回复一", root=1),), 2, 2),
            (1, 2): SubReplyPage((comment(3, "回复二", root=1, rank=20),), None, 2),
        },
    )
    _, count = crawl_target(
        "BV1xx411c7mD", source, database, replies_mode="all", comment_order="time"
    )
    rows = list(database.iter_comments("BV1xx411c7mD"))
    assert count == 3
    assert [row["rpid"] for row in rows] == [1, 2, 3]
    assert rows[1]["root_rpid"] == 1
    assert rows[1]["level"] == 1
    assert database.get_run(1)["status"] == "completed"
    database.close()


class FailingSubSource(FakeSource):
    def get_sub_reply_page(
        self, aid: int, root_rpid: int, page: int
    ) -> SubReplyPage:
        if page == 2:
            raise ApiError("subreply failure")
        return super().get_sub_reply_page(aid, root_rpid, page)


def test_subreply_run_resumes_from_independent_page_checkpoint(tmp_path) -> None:
    database = Database(tmp_path / "comments.db")
    failing = FailingSubSource(
        [CommentPage((comment(1, "根", replies=2),), None)],
        [""],
        {(1, 1): SubReplyPage((comment(2, "回复一", root=1),), 2, 2)},
    )
    with pytest.raises(ApiError):
        crawl_target("BV1xx411c7mD", failing, database, replies_mode="all")
    assert database.get_run(1)["sub_page"] == 2

    resumed = FakeSource(
        [],
        [],
        {(1, 2): SubReplyPage((comment(3, "回复二", root=1, rank=20),), None, 2)},
    )
    _, count = crawl_target(
        "BV1xx411c7mD", resumed, database, replies_mode="all"
    )
    assert count == 3
    assert database.get_run(1)["status"] == "completed"
    database.close()


class FailingSource(FakeSource):
    def get_comment_page(
        self, aid: int, cursor: str = "", *, order: str = "hot"
    ) -> CommentPage:
        if cursor == "next":
            raise ApiError("temporary failure")
        return super().get_comment_page(aid, cursor, order=order)


def test_failed_run_resumes_from_saved_cursor(tmp_path) -> None:
    database = Database(tmp_path / "comments.db")
    failing = FailingSource(
        [CommentPage((comment(1, "已保存"),), "next")], [""]
    )
    with pytest.raises(ApiError):
        crawl_target("BV1xx411c7mD", failing, database)

    resumed = FakeSource([CommentPage((comment(2, "续抓"),), None)], ["next"])
    _, count = crawl_target("BV1xx411c7mD", resumed, database)
    assert count == 2
    assert [row["rpid"] for row in database.iter_comments("BV1xx411c7mD")] == [1, 2]
    database.close()
