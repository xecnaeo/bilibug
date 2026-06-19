from pathlib import Path

import pytest

from bili_comments.cli import main
from bili_comments import content
from bili_comments.database import Database
from bili_comments.models import Comment, Video, VideoStats
from bili_comments.report import generate_report

BVID = "BV1xx411c7mD"


def _comment(rpid: int, *, replies: int = 0, level: int = 0) -> Comment:
    return Comment(
        rpid=rpid,
        message="不应出现在报告中的评论正文",
        ctime=1700000000 + rpid,
        like_count=12,
        reply_count=replies,
        author_mid="private-mid",
        author_name="不应出现的昵称",
        author_level=5,
        root_rpid=1 if level else 0,
        parent_rpid=1 if level else 0,
        level=level,
    )


def _prepared_database(path: Path, *, observation_count: int = 3) -> Database:
    database = Database(path)
    for index in range(observation_count):
        video = Video(
            aid=1,
            bvid=BVID,
            title="标题 <测试> & 数据",
            owner_mid="owner-mid",
            owner_name="不应出现的作者",
            category_id=2197,
            published_at=1699000000,
            stats=VideoStats(
                view=100 + index * 10,
                reply=3,
                favorite=10 + index,
                coin=5 + index,
                share=2 + index,
                like=20 + index,
            ),
        )
        database.upsert_video(video, BVID)
        run = database.start_or_resume_run(
            BVID, source="test", comment_order="time", replies_mode="root"
        )
        database.save_video_observation(int(run["id"]), video)
        database.save_root_page(
            int(run["id"]),
            BVID,
            [_comment(1, replies=2), _comment(2)],
            rank_start=0,
            next_cursor=None,
            sort_order="time",
            replies_mode="root",
        )
        observed_at = f"2026-06-{17 + index:02d}T08:00:00+00:00"
        database.connection.execute(
            "UPDATE video_observations SET observed_at = ? WHERE run_id = ?",
            (observed_at, run["id"]),
        )
    database.connection.execute(
        """
        INSERT INTO comments (
            bvid, rpid, message, ctime, like_count, reply_count, hot_rank,
            sort_order, sort_rank, root_rpid, parent_rpid, level, pin_type,
            state, author_mid, author_name, author_level, first_seen_at, last_seen_at
        ) VALUES (?, 3, ?, 1700000003, 1, 0, 0, 'thread', 0, 1, 1, 1,
                  '', 0, ?, ?, 4, ?, ?)
        """,
        (
            BVID,
            "子回复私密正文",
            "child-mid",
            "子回复昵称",
            "2026-06-19T08:00:00+00:00",
            "2026-06-19T08:00:00+00:00",
        ),
    )
    database.connection.execute(
        """
        UPDATE crawl_runs SET status = 'failed', completeness = 'partial',
               end_reason = 'error', error = '请求触发风控：<code>'
        WHERE id = (SELECT MAX(id) FROM crawl_runs)
        """
    )
    database.connection.commit()
    return database


def test_report_contains_aggregates_and_escapes_private_data(tmp_path) -> None:
    database = _prepared_database(tmp_path / "db.sqlite")
    output = tmp_path / "nested" / "report.html"
    assert generate_report(database, [BVID], output) == 1
    html = output.read_text(encoding="utf-8")
    assert "标题 &lt;测试&gt; &amp; 数据" in html
    assert "missing_category_name" in html
    assert "可用于趋势观察" in html
    assert "楼中楼不完整" in html
    assert "请求触发风控：&lt;code&gt;" in html
    assert "不应出现在报告中的评论正文" not in html
    assert "private-mid" not in html
    assert "不应出现的昵称" not in html
    assert "http://" not in html and "https://" not in html
    assert "一级评论内容分析" not in html
    assert html.count("trend-line") >= 1
    database.close()


@pytest.mark.parametrize("observation_count", (1, 2))
def test_insufficient_observations_are_not_called_a_trend(
    tmp_path, observation_count
) -> None:
    database = _prepared_database(
        tmp_path / "db.sqlite", observation_count=observation_count
    )
    if observation_count == 2:
        database.connection.execute(
            "UPDATE video_observations SET observed_at = '2026-06-19T08:10:00+00:00' WHERE run_id = 2"
        )
    database.connection.commit()
    output = tmp_path / "report.html"
    generate_report(database, [BVID], output)
    html = output.read_text(encoding="utf-8")
    assert "样本不足" in html
    assert "不能判断趋势" in html
    assert "可用于趋势观察" not in html
    database.close()


def test_report_cli_all_targets_and_configuration_errors(tmp_path, capsys) -> None:
    path = tmp_path / "db.sqlite"
    database = _prepared_database(path)
    database.close()
    output = tmp_path / "report.html"
    assert main(["--db", str(path), "report", "--output", str(output)]) == 0
    assert BVID in output.read_text(encoding="utf-8")
    selected = tmp_path / "selected.html"
    assert (
        main(
            ["--db", str(path), "report", BVID, "--output", str(selected)]
        )
        == 0
    )
    assert BVID in selected.read_text(encoding="utf-8")

    assert (
        main(
            [
                "--db",
                str(path),
                "report",
                "BV1yy411c7mE",
                "--output",
                str(tmp_path / "missing.html"),
            ]
        )
        == 2
    )
    empty = tmp_path / "empty.sqlite"
    assert main(["--db", str(empty), "report", "--output", str(output)]) == 2
    assert "数据库中没有" in capsys.readouterr().err


def test_content_report_uses_only_root_comments(tmp_path, monkeypatch) -> None:
    class FakeJieba:
        __version__ = "test"

        @staticmethod
        def lcut(sentence: str, cut_all: bool = False) -> list[str]:
            return sentence.split()

    database = _prepared_database(tmp_path / "db.sqlite")
    database.connection.execute(
        "UPDATE comments SET message = '一级 主题' WHERE bvid = ? AND level = 0",
        (BVID,),
    )
    database.connection.execute(
        """
        INSERT INTO comments (
            bvid, rpid, message, ctime, like_count, reply_count, hot_rank,
            sort_order, sort_rank, root_rpid, parent_rpid, level, pin_type,
            state, author_mid, author_name, author_level, first_seen_at, last_seen_at
        ) VALUES (?, 4, '一级 主题', 1700000004, 1, 0, 0, 'time', 3, 0, 0, 0,
                  '', 0, 'root-mid', 'root-name', 4, ?, ?)
        """,
        (BVID, "2026-06-19T08:00:00+00:00", "2026-06-19T08:00:00+00:00"),
    )
    database.connection.execute(
        "UPDATE comments SET message = '子回复词 子回复词 子回复词' WHERE level = 1"
    )
    database.connection.commit()
    monkeypatch.setattr(content, "_load_jieba", lambda: FakeJieba())
    output = tmp_path / "report.html"
    generate_report(database, [BVID], output, content_analysis=True)
    html = output.read_text(encoding="utf-8")
    assert "一级评论内容分析" in html
    assert "jieba test" in html
    assert "主题" in html
    assert "子回复词" not in html
    assert "一级 主题" not in html
    database.close()


def test_trend_window_and_gap_detection(tmp_path) -> None:
    database = _prepared_database(tmp_path / "db.sqlite")
    database.connection.execute(
        "UPDATE video_observations SET observed_at = '2026-06-17T09:00:00+00:00' WHERE run_id = 2"
    )
    database.connection.commit()
    output = tmp_path / "report.html"
    generate_report(database, [BVID], output, days=2)
    html = output.read_text(encoding="utf-8")
    assert "最近 2 天有 3 个观测点" in html
    assert "1 个超过 36 小时的数据缺口" in html
    assert "数据缺口 1" in html
    database.close()


def test_no_valid_recent_observations_keeps_latest_metrics(tmp_path) -> None:
    database = _prepared_database(tmp_path / "db.sqlite", observation_count=1)
    database.connection.execute("UPDATE video_observations SET observed_at = 'invalid'")
    database.connection.commit()
    output = tmp_path / "report.html"
    generate_report(database, [BVID], output)
    html = output.read_text(encoding="utf-8")
    assert "最近 7 天没有可用观测" in html
    assert ">100<" in html
    database.close()


def test_report_cli_content_dependency_and_invalid_days(
    tmp_path, monkeypatch, capsys
) -> None:
    path = tmp_path / "db.sqlite"
    database = _prepared_database(path)
    database.close()

    def missing(_name: str):
        raise ImportError

    monkeypatch.setattr(content.importlib, "import_module", missing)
    assert (
        main(
            [
                "--db",
                str(path),
                "report",
                "--content-analysis",
                "--output",
                str(tmp_path / "report.html"),
            ]
        )
        == 2
    )
    assert "[analysis]" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "--db",
                str(path),
                "report",
                "--days",
                "0",
                "--output",
                str(tmp_path / "invalid.html"),
            ]
        )
    assert exc_info.value.code == 2
