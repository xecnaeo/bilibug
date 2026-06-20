import pytest

from bili_comments.lifecycle import HOUR
from bili_comments.topic_evolution import analyze_topic_evolution

PUBLISHED = 1_700_000_000


class FakeJieba:
    __version__ = "test"

    @staticmethod
    def lcut(sentence: str, cut_all: bool = False) -> list[str]:
        assert cut_all is False
        return sentence.split()


def item(hours: float, message: str) -> dict[str, object]:
    return {
        "ctime": PUBLISHED + int(hours * HOUR),
        "message": message,
        "level": 0,
    }


def test_fixed_stage_boundaries() -> None:
    comments = [
        item(0, "起点 主题"),
        item(5.999, "早期 主题"),
        item(6, "六小时 主题"),
        item(23.999, "当天 主题"),
        item(24, "一天 主题"),
        item(71.999, "三天前 主题"),
        item(72, "三天 主题"),
        item(167.999, "七天前 主题"),
        item(168, "长尾 主题"),
    ]
    result = analyze_topic_evolution(
        comments,
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 14 * 24 * HOUR,
        jieba=FakeJieba(),
    )
    assert result is not None
    assert [stage.comment_count for stage in result.stages] == [2, 2, 2, 2, 1]
    assert [stage.status for stage in result.stages] == [
        "complete",
        "complete",
        "complete",
        "complete",
        "ongoing",
    ]


def test_stage_maturity_and_minimum_comment_threshold() -> None:
    comments = [item(1, "早期 主题") for _ in range(20)]
    comments.extend(item(8, "进行 主题") for _ in range(20))
    result = analyze_topic_evolution(
        comments,
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 12 * HOUR,
        jieba=FakeJieba(),
    )
    assert result is not None
    assert result.stages[0].eligible
    assert result.stages[1].status == "ongoing"
    assert not result.stages[1].eligible
    assert result.stages[-1].status == "ongoing"
    assert not result.stages[-1].eligible

    below = analyze_topic_evolution(
        [item(1, "早期 主题") for _ in range(19)],
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 8 * HOUR,
        jieba=FakeJieba(),
    )
    assert below is not None
    assert not below.stages[0].eligible


def test_topic_heatmap_new_and_persistent_terms_are_deterministic() -> None:
    stage_data = (
        (1, "持续 萌芽"),
        (8, "持续 扩散"),
        (30, "持续 讨论"),
        (80, "持续 转折"),
        (200, "持续 长尾"),
    )
    comments = [
        item(hours, message)
        for hours, message in stage_data
        for _ in range(20)
    ]
    first = analyze_topic_evolution(
        comments,
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 14 * 24 * HOUR,
        jieba=FakeJieba(),
    )
    second = analyze_topic_evolution(
        comments,
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 14 * 24 * HOUR,
        jieba=FakeJieba(),
    )
    assert first is not None and first == second
    assert first.comparable
    assert first.persistent_terms == ("持续",)
    assert "扩散" in first.stages[1].new_terms
    assert "萌芽" not in first.stages[1].new_terms
    assert {term.token for term in first.heatmap_terms} == {
        "持续",
        "萌芽",
        "扩散",
        "讨论",
        "转折",
        "长尾",
    }
    assert all(0 <= value <= 1 for term in first.heatmap_terms for value in term.weights)


def test_global_single_token_privacy_block_applies_across_stages() -> None:
    comments = [{"ctime": "bad", "message": "隐私", "level": 0}]
    comments.extend(item(1, "隐私 早期") for _ in range(20))
    comments.extend(item(30, "隐私 后期") for _ in range(20))
    result = analyze_topic_evolution(
        comments,
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 4 * 24 * HOUR,
        jieba=FakeJieba(),
    )
    assert result is not None
    assert all(
        keyword.token != "隐私"
        for stage in result.stages
        for keyword in stage.keywords
    )


def test_invalid_comments_and_missing_video_time() -> None:
    comments = [
        item(1, "有效 主题"),
        {"ctime": PUBLISHED - 1, "message": "过早", "level": 0},
        {"ctime": PUBLISHED + 20 * HOUR, "message": "未来", "level": 0},
        {"ctime": "bad", "message": "无效", "level": 0},
        {"ctime": PUBLISHED + HOUR, "message": "子回复", "level": 1},
    ]
    result = analyze_topic_evolution(
        comments,
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 10 * HOUR,
        jieba=FakeJieba(),
    )
    assert result is not None
    assert result.total_comment_count == 1
    assert result.invalid_comment_count == 3
    assert analyze_topic_evolution(
        comments, published_at=0, cutoff_at=PUBLISHED, jieba=FakeJieba()
    ) is None
