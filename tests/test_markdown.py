from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from bili_comments.cli import main
from bili_comments.database import Database
from bili_comments.errors import ConfigurationError
from bili_comments.markdown import (
    CommentRecord,
    export_markdown_corpus,
    select_noteworthy,
)
from bili_comments.models import Comment, Video

BVID = "BV1xx411c7mD"


def record(
    rpid: int,
    *,
    message: str = "普通评论",
    likes: int = 0,
    replies: int = 0,
    pin: str = "",
) -> CommentRecord:
    return CommentRecord(
        rpid=rpid,
        message=message,
        ctime=1_700_000_000 + rpid,
        like_count=likes,
        reply_count=replies,
        sort_order="hot",
        sort_rank=rpid - 1,
        pin_type=pin,
    )


def _prepared_database(path: Path) -> None:
    with Database(path) as database:
        database.upsert_video(
            Video(
                aid=1,
                bvid=BVID,
                title="标题 <测试>",
                owner_mid="owner-secret",
                owner_name="作者秘密",
                published_at=1_700_000_000,
            ),
            BVID,
        )
        run = database.start_or_resume_run(
            BVID,
            source="test",
            comment_order="hot",
            replies_mode="root",
        )
        comments = [
            Comment(
                rpid=index,
                message=(
                    "重复内容" if index <= 3 else
                    "资料 https://example.com/<script>" if index == 4 else
                    "很长的观点\n" + "内容" * 60 if index == 5 else
                    f"评论 {index}"
                ),
                ctime=1_700_000_000 + index * 60,
                like_count=index + 5,
                reply_count=10 if index == 6 else index % 3,
                author_mid="123456789",
                author_name="秘密昵称",
                author_level=6,
                pin_type="upper" if index == 7 else "",
                sort_rank=index - 1,
            )
            for index in range(1, 41)
        ]
        database.save_root_page(
            int(run["id"]),
            BVID,
            comments,
            rank_start=0,
            next_cursor=None,
            sort_order="hot",
            replies_mode="root",
        )


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_multichannel_selection_is_deterministic_and_deduplicated() -> None:
    comments = [
        record(index, message=f"评论 {index}", likes=index + 5, replies=index % 3)
        for index in range(1, 41)
    ]
    comments[0] = record(1, message="重复 内容", likes=6)
    comments[1] = record(2, message="重复\n内容", likes=20)
    comments[2] = record(3, message="重复   内容", likes=10)
    comments[3] = record(4, message="资料 https://example.com", likes=8)
    comments[4] = record(5, message="观点" * 100, likes=1)
    comments[5] = record(6, likes=5, replies=50)
    comments[6] = record(7, likes=0, replies=0, pin="upper")

    first = select_noteworthy(comments)
    second = select_noteworthy(comments)
    assert first == second
    assert len({item.comment.rpid for item in first}) == len(first)
    assert sum("高互动" in item.reasons for item in first) == 30
    assert "高回复" in next(item.reasons for item in first if item.comment.rpid == 6)
    assert "长篇观点" in next(item.reasons for item in first if item.comment.rpid == 5)
    assert "资料链接" in next(item.reasons for item in first if item.comment.rpid == 4)
    assert "置顶" in next(item.reasons for item in first if item.comment.rpid == 7)
    repeated = next(item for item in first if "重复共鸣" in item.reasons)
    assert repeated.comment.rpid == 2
    assert repeated.duplicate_count == 3


def test_selection_caps_high_engagement_and_reply_channels() -> None:
    comments = [
        record(index, message=f"唯一评论 {index}", likes=index + 5, replies=index)
        for index in range(1, 4_001)
    ]
    selected = select_noteworthy(comments)
    assert sum("高互动" in item.reasons for item in selected) == 300
    assert sum("高回复" in item.reasons for item in selected) == 50


def test_export_creates_safe_corpus_without_modifying_database(tmp_path) -> None:
    database_path = tmp_path / "source.db"
    _prepared_database(database_path)
    before = _hash(database_path)
    output = tmp_path / "corpus"

    manifest = export_markdown_corpus(database_path, [], output)

    assert _hash(database_path) == before
    assert manifest["video_count"] == 1
    assert manifest["comment_count"] == 40
    assert manifest["selected_count"] >= 30
    assert manifest["selection_rule"] == "noteworthy-v1"
    document = (output / "documents" / f"{BVID}.md").read_text(encoding="utf-8")
    index = (output / "index.md").read_text(encoding="utf-8")
    persisted = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert document.startswith("# 标题 &lt;测试&gt;")
    assert "## 评论 0001 · rpid" in document
    assert "### 原始正文" in document
    assert "&lt;script&gt;" in document
    assert "秘密昵称" not in document
    assert "123456789" not in document
    assert str(database_path) not in document + index + json.dumps(persisted)
    assert persisted["files"][0]["sha256"] == _hash(output / "documents" / f"{BVID}.md")


def test_export_rejects_unknown_target_and_nonempty_output(tmp_path) -> None:
    database_path = tmp_path / "source.db"
    _prepared_database(database_path)
    with pytest.raises(ConfigurationError, match="数据库中没有视频"):
        export_markdown_corpus(database_path, ["BV1unknown000"], tmp_path / "unknown")
    assert not (tmp_path / "unknown").exists()

    output = tmp_path / "existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="输出目录必须为空"):
        export_markdown_corpus(database_path, [], output)
    assert marker.read_text(encoding="utf-8") == "keep"


def test_export_rejects_empty_and_incompatible_databases(tmp_path) -> None:
    empty = tmp_path / "empty.db"
    with Database(empty):
        pass
    with pytest.raises(ConfigurationError, match="没有可导出"):
        export_markdown_corpus(empty, [], tmp_path / "empty-output")
    assert not (tmp_path / "empty-output").exists()

    incompatible = tmp_path / "incompatible.db"
    connection = sqlite3.connect(incompatible)
    connection.execute("PRAGMA user_version = 3")
    connection.execute("CREATE TABLE videos (bvid TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()
    with pytest.raises(ConfigurationError, match="videos.*缺少字段"):
        export_markdown_corpus(incompatible, [], tmp_path / "bad-output")


def test_document_output_is_deterministic(tmp_path) -> None:
    database_path = tmp_path / "source.db"
    _prepared_database(database_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    export_markdown_corpus(database_path, [], first)
    export_markdown_corpus(database_path, [], second)
    assert (first / "documents" / f"{BVID}.md").read_bytes() == (
        second / "documents" / f"{BVID}.md"
    ).read_bytes()


def test_cli_exports_selected_target(tmp_path, capsys) -> None:
    database_path = tmp_path / "source.db"
    _prepared_database(database_path)
    output = tmp_path / "corpus"
    assert main(
        [
            "--db",
            str(database_path),
            "markdown",
            BVID,
            "--output-dir",
            str(output),
        ]
    ) == 0
    assert "已生成 1 个视频" in capsys.readouterr().out
    assert (output / "documents" / f"{BVID}.md").is_file()
