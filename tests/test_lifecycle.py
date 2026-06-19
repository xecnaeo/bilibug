import pytest

from bili_comments.lifecycle import (
    DAY,
    HOUR,
    analyze_lifecycle,
    analyze_threads,
    compare_lifecycles,
)

PUBLISHED = 1_700_000_000


def row(
    rpid: int,
    hours: float,
    *,
    level: int = 0,
    root_rpid: int = 0,
    likes: int = 0,
    replies: int = 0,
) -> dict[str, object]:
    return {
        "rpid": rpid,
        "ctime": PUBLISHED + int(hours * HOUR),
        "level": level,
        "root_rpid": root_rpid,
        "like_count": likes,
        "reply_count": replies,
    }


def test_lifecycle_buckets_quantiles_peak_and_long_tail() -> None:
    hours = [1, 1.5, 2, 3, 4, 24, 48, 72, 120, 200]
    comments = [row(index, value) for index, value in enumerate(hours, 1)]
    analysis = analyze_lifecycle(
        comments,
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 10 * DAY,
    )
    assert analysis is not None
    assert analysis.root_count == 10
    assert analysis.peak_hour == 1
    assert analysis.peak_count == 2
    assert analysis.t50_hours == 4
    assert analysis.t80_hours == 72
    assert analysis.t90_hours == 120
    assert analysis.first_day_share == pytest.approx(0.5)
    assert analysis.first_three_days_share == pytest.approx(0.7)
    assert analysis.first_week_share == pytest.approx(0.9)
    assert analysis.long_tail_share == pytest.approx(0.1)
    assert len(analysis.hourly_counts) == 24
    assert sum(bucket.root_count for bucket in analysis.hourly_counts) == 5
    assert len(analysis.daily_counts) == 10


def test_lifecycle_excludes_invalid_times_and_omits_future_buckets() -> None:
    comments = [
        row(1, 1),
        {**row(2, 1), "ctime": PUBLISHED - 1},
        {**row(3, 1), "ctime": PUBLISHED + 5 * HOUR},
        {**row(4, 1), "ctime": 0},
    ]
    analysis = analyze_lifecycle(
        comments,
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 3 * HOUR,
    )
    assert analysis is not None
    assert analysis.root_count == 1
    assert analysis.invalid_before_publish == 1
    assert analysis.invalid_after_cutoff == 1
    assert analysis.invalid_timestamp == 1
    assert len(analysis.hourly_counts) == 3
    assert all(bucket.end_hours <= 3 for bucket in analysis.hourly_counts)
    assert analyze_lifecycle(comments, published_at=0, cutoff_at=PUBLISHED) is None


def test_common_mature_comparison_window() -> None:
    first = analyze_lifecycle(
        [row(1, 1), row(2, 5), row(3, 9)],
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 10 * HOUR,
    )
    second = analyze_lifecycle(
        [row(4, 2), row(5, 4), row(6, 8)],
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 20 * HOUR,
    )
    assert first is not None and second is not None
    comparison = compare_lifecycles((("A", first), ("B", second)))
    assert comparison is not None
    assert comparison.horizon_hours == 10
    assert [item.comment_count for item in comparison.series] == [3, 3]
    assert comparison.series[0].t50_hours == 5

    young = analyze_lifecycle(
        [row(7, 1)],
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 5 * HOUR,
    )
    assert young is not None
    assert compare_lifecycles((("A", first), ("Y", young))) is None


@pytest.mark.parametrize(
    "actual,complete,expected",
    [(8, True, False), (9, True, True), (10, False, False)],
)
def test_thread_time_analysis_coverage_gate(actual, complete, expected) -> None:
    roots = [row(1, 1, replies=10, likes=10)]
    children = [
        row(100 + index, 2 + index, level=1, root_rpid=1)
        for index in range(actual)
    ]
    analysis = analyze_threads(
        roots + children,
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 20 * HOUR,
        complete_all_run=complete,
    )
    assert analysis.time_analysis_enabled is expected
    assert analysis.coverage == pytest.approx(actual / 10)
    assert analysis.static_top_ten_share == 1


def test_thread_migration_concentration_and_delays() -> None:
    roots = [
        row(1, 1, replies=10, likes=100),
        row(2, 2, replies=0, likes=1),
    ]
    children = [
        *[row(100 + index, 5.1, level=1, root_rpid=1) for index in range(5)],
        *[row(200 + index, 6.1, level=1, root_rpid=1) for index in range(5)],
    ]
    analysis = analyze_threads(
        roots + children,
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 12 * HOUR,
        complete_all_run=True,
    )
    assert analysis.time_analysis_enabled
    assert analysis.migration_point_hours == 5
    assert analysis.high_like_root_share == pytest.approx(0.5)
    assert analysis.high_like_child_share == 1
    assert analysis.actual_top_ten_share == 1
    assert analysis.reply_delay_median_hours == pytest.approx(4.1, abs=0.01)
    assert analysis.reply_delay_p90_hours == pytest.approx(4.1, abs=0.01)


def test_invalid_reply_delays_are_excluded() -> None:
    roots = [row(1, 3, replies=2, likes=5)]
    children = [
        row(2, 2, level=1, root_rpid=1),
        row(3, 4, level=1, root_rpid=999),
    ]
    analysis = analyze_threads(
        roots + children,
        published_at=PUBLISHED,
        cutoff_at=PUBLISHED + 6 * HOUR,
        complete_all_run=True,
    )
    assert analysis.time_analysis_enabled
    assert analysis.invalid_reply_delays == 2
    assert analysis.reply_delay_median_hours is None
