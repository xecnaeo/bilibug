import json

import pytest

from bili_comments.batch import parse_manifest, resume_batch, run_manifest
from bili_comments.cli import main
from bili_comments.database import Database
from bili_comments.errors import ApiError, ConfigurationError
from bili_comments.models import CommentPage, SubReplyPage, Video

BVIDS = ("BV1xx411c7mD", "BV1yy411c7mE", "BV1zz411c7mF")


class FakeBatchSource:
    name = "fake-batch"

    def __init__(self, failed: set[str] | None = None) -> None:
        self.failed = failed or set()
        self.calls: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def close(self) -> None:
        pass

    def get_video(self, bvid: str) -> Video:
        self.calls.append(bvid)
        if bvid in self.failed:
            raise ApiError("模拟失败")
        return Video(abs(hash(bvid)), bvid, "标题", "2", "作者")

    def get_comment_page(
        self, aid: int, cursor: str = "", *, order: str = "hot"
    ) -> CommentPage:
        return CommentPage((), None)

    def get_sub_reply_page(
        self, aid: int, root_rpid: int, page: int
    ) -> SubReplyPage:
        raise AssertionError("root mode must not fetch sub replies")


def write_manifest(path, rows: str) -> None:
    path.write_text("target,order,replies,enabled\n" + rows, encoding="utf-8")


def test_manifest_defaults_disabled_rows_and_utf8_path(tmp_path) -> None:
    path = tmp_path / "中文清单.csv"
    write_manifest(path, f"{BVIDS[0]},,,true\n,hot,all,false\n")
    manifest = parse_manifest(path)
    assert manifest.items[0].comment_order == "time"
    assert manifest.items[0].replies_mode == "root"
    assert manifest.items[0].row_number == 2
    assert len(manifest.sha256) == 64


@pytest.mark.parametrize(
    "rows, message",
    [
        ("", "没有启用"),
        (f"{BVIDS[0]},bad,root,true\n", "order"),
        (f"{BVIDS[0]},time,bad,true\n", "replies"),
        (f"{BVIDS[0]},time,root,true\n{BVIDS[0]},hot,all,true\n", "重复"),
        ("not-a-video,time,root,true\n", "无法从目标中识别"),
    ],
)
def test_manifest_rejects_invalid_input(tmp_path, rows, message) -> None:
    path = tmp_path / "targets.csv"
    write_manifest(path, rows)
    with pytest.raises(ConfigurationError, match=message):
        parse_manifest(path)


def test_batch_continues_after_failure_and_resume_uses_snapshot(tmp_path) -> None:
    path = tmp_path / "targets.csv"
    write_manifest(
        path,
        "\n".join(f"{bvid},time,root,true" for bvid in BVIDS) + "\n",
    )
    summary = tmp_path / "summary.json"
    database = Database(tmp_path / "db.sqlite")
    first_source = FakeBatchSource({BVIDS[1]})
    details, exit_code = run_manifest(
        database, first_source, path, summary_path=summary
    )
    assert exit_code == 1
    assert first_source.calls == list(BVIDS)
    assert details["batch"]["succeeded_count"] == 2
    assert details["batch"]["failed_count"] == 1
    assert [item["status"] for item in details["items"]] == [
        "succeeded",
        "failed",
        "succeeded",
    ]
    saved = json.loads(summary.read_text(encoding="utf-8"))
    assert "comments" not in saved

    path.write_text("target\nnot-a-video\n", encoding="utf-8")
    resumed_source = FakeBatchSource()
    resumed, exit_code = resume_batch(
        database,
        resumed_source,
        int(details["batch"]["id"]),
        summary_path=summary,
    )
    assert exit_code == 0
    assert resumed_source.calls == [BVIDS[1]]
    assert resumed["batch"]["succeeded_count"] == 3
    assert resumed["batch"]["failed_count"] == 0
    database.close()


def test_resume_resets_interrupted_item_to_pending(tmp_path) -> None:
    path = tmp_path / "targets.csv"
    write_manifest(path, f"{BVIDS[0]},time,root,true\n")
    manifest = parse_manifest(path)
    database = Database(tmp_path / "db.sqlite")
    batch = database.create_batch_run(
        str(path), manifest.sha256, (item.database_values() for item in manifest.items)
    )
    item = list(database.iter_batch_items(batch["id"]))[0]
    database.mark_batch_item_running(item["id"])
    database.prepare_batch_resume(batch["id"])
    assert list(database.iter_batch_items(batch["id"]))[0]["status"] == "pending"
    database.close()


def test_cli_returns_two_for_manifest_error_and_one_for_partial_batch(
    tmp_path, monkeypatch
) -> None:
    invalid = tmp_path / "invalid.csv"
    invalid.write_text("wrong\nvalue\n", encoding="utf-8")
    db_path = tmp_path / "db.sqlite"
    assert main(["--db", str(db_path), "batch", "run", str(invalid)]) == 2

    valid = tmp_path / "valid.csv"
    write_manifest(valid, f"{BVIDS[0]},time,root,true\n")
    source = FakeBatchSource({BVIDS[0]})
    monkeypatch.setattr("bili_comments.cli.BilibiliWebSource", lambda: source)
    summary = tmp_path / "summary.json"
    assert main(
        [
            "--db",
            str(db_path),
            "batch",
            "run",
            str(valid),
            "--summary",
            str(summary),
        ]
    ) == 1
