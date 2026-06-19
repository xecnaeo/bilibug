from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from statistics import median
from typing import Mapping, Sequence

HOUR = 60 * 60
DAY = 24 * HOUR
MAX_COMPARISON_HOURS = 7 * 24
MIN_COMPARISON_HOURS = 6
MIN_THREAD_COVERAGE = 0.90
MIN_MIGRATION_ACTIONS = 5


@dataclass(frozen=True)
class CountBucket:
    start_hours: float
    end_hours: float
    root_count: int
    child_count: int = 0
    complete: bool = True

    @property
    def width_hours(self) -> float:
        return self.end_hours - self.start_hours

    @property
    def total_count(self) -> int:
        return self.root_count + self.child_count


@dataclass(frozen=True)
class LifecycleAnalysis:
    published_at: int
    cutoff_at: int
    age_hours: float
    root_elapsed_hours: tuple[float, ...]
    invalid_before_publish: int
    invalid_after_cutoff: int
    invalid_timestamp: int
    hourly_counts: tuple[CountBucket, ...]
    daily_counts: tuple[CountBucket, ...]
    cumulative_7d: tuple[tuple[float, float], ...]
    peak_hour: int | None
    peak_count: int
    t50_hours: float | None
    t80_hours: float | None
    t90_hours: float | None
    first_day_share: float | None
    first_three_days_share: float | None
    first_week_share: float | None
    long_tail_share: float | None

    @property
    def root_count(self) -> int:
        return len(self.root_elapsed_hours)


@dataclass(frozen=True)
class ComparisonSeries:
    bvid: str
    comment_count: int
    t50_hours: float
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class LifecycleComparison:
    horizon_hours: int
    series: tuple[ComparisonSeries, ...]


@dataclass(frozen=True)
class ThreadAnalysis:
    declared_children: int
    actual_children: int
    coverage: float | None
    complete_all_run: bool
    time_analysis_enabled: bool
    static_top_ten_share: float | None
    high_like_root_share: float | None
    high_like_child_share: float | None
    actual_top_ten_share: float | None
    migration_buckets: tuple[CountBucket, ...]
    migration_point_hours: float | None
    reply_delay_median_hours: float | None
    reply_delay_p90_hours: float | None
    invalid_reply_delays: int


def _elapsed_hours(timestamp: object, published_at: int, cutoff_at: int) -> tuple[float | None, str | None]:
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return None, "invalid"
    if value <= 0:
        return None, "invalid"
    if value < published_at:
        return None, "before"
    if value > cutoff_at:
        return None, "after"
    return (value - published_at) / HOUR, None


def _quantile_elapsed(values: Sequence[float], ratio: float) -> float | None:
    if not values:
        return None
    index = max(0, math.ceil(len(values) * ratio) - 1)
    return sorted(values)[index]


def _share_within(values: Sequence[float], hours: float) -> float | None:
    if not values:
        return None
    return sum(value < hours for value in values) / len(values)


def _fixed_buckets(
    values: Sequence[float], *, age_hours: float, width_hours: int, limit_hours: int
) -> tuple[CountBucket, ...]:
    visible_hours = min(max(0.0, age_hours), float(limit_hours))
    bucket_count = math.ceil(visible_hours / width_hours)
    counts = Counter(int(value // width_hours) for value in values if 0 <= value < limit_hours)
    return tuple(
        CountBucket(
            start_hours=index * width_hours,
            end_hours=(index + 1) * width_hours,
            root_count=counts[index],
            complete=(index + 1) * width_hours <= age_hours,
        )
        for index in range(bucket_count)
    )


def analyze_lifecycle(
    comments: Sequence[Mapping[str, object]], *, published_at: int, cutoff_at: int
) -> LifecycleAnalysis | None:
    if published_at <= 0 or cutoff_at < published_at:
        return None
    elapsed: list[float] = []
    invalid = Counter()
    for row in comments:
        if int(row["level"]) != 0:
            continue
        value, reason = _elapsed_hours(row["ctime"], published_at, cutoff_at)
        if reason:
            invalid[reason] += 1
        elif value is not None:
            elapsed.append(value)
    elapsed.sort()
    age_hours = (cutoff_at - published_at) / HOUR
    hourly_counts = _fixed_buckets(
        elapsed, age_hours=age_hours, width_hours=1, limit_hours=24
    )
    daily_counts = _fixed_buckets(
        elapsed, age_hours=age_hours, width_hours=24, limit_hours=30 * 24
    )
    cumulative_limit = min(age_hours, float(MAX_COMPARISON_HOURS))
    cumulative_total = len(elapsed) or 1
    cumulative_7d = tuple(
        (
            hour,
            sum(value < hour for value in elapsed) / cumulative_total,
        )
        for hour in range(1, math.floor(cumulative_limit) + 1)
    )
    peak_counts = Counter(int(value // 1) for value in elapsed)
    peak_hour = None
    peak_count = 0
    if peak_counts:
        peak_count = max(peak_counts.values())
        peak_hour = min(hour for hour, count in peak_counts.items() if count == peak_count)
    first_week_share = _share_within(elapsed, 7 * 24)
    return LifecycleAnalysis(
        published_at=published_at,
        cutoff_at=cutoff_at,
        age_hours=age_hours,
        root_elapsed_hours=tuple(elapsed),
        invalid_before_publish=invalid["before"],
        invalid_after_cutoff=invalid["after"],
        invalid_timestamp=invalid["invalid"],
        hourly_counts=hourly_counts,
        daily_counts=daily_counts,
        cumulative_7d=cumulative_7d,
        peak_hour=peak_hour,
        peak_count=peak_count,
        t50_hours=_quantile_elapsed(elapsed, 0.50),
        t80_hours=_quantile_elapsed(elapsed, 0.80),
        t90_hours=_quantile_elapsed(elapsed, 0.90),
        first_day_share=_share_within(elapsed, 24),
        first_three_days_share=_share_within(elapsed, 3 * 24),
        first_week_share=first_week_share,
        long_tail_share=None if first_week_share is None else 1 - first_week_share,
    )


def compare_lifecycles(
    analyses: Sequence[tuple[str, LifecycleAnalysis]],
) -> LifecycleComparison | None:
    eligible = [(bvid, item) for bvid, item in analyses if item.root_count > 0]
    if len(eligible) < 2:
        return None
    horizon = min(
        MAX_COMPARISON_HOURS,
        math.floor(min(item.age_hours for _, item in eligible)),
    )
    if horizon < MIN_COMPARISON_HOURS:
        return None
    series = []
    for bvid, item in eligible:
        values = [value for value in item.root_elapsed_hours if value < horizon]
        if not values:
            continue
        values.sort()
        total = len(values)
        points = tuple(
            (hour, sum(value < hour for value in values) / total)
            for hour in range(1, horizon + 1)
        )
        series.append(
            ComparisonSeries(
                bvid=bvid,
                comment_count=total,
                t50_hours=_quantile_elapsed(values, 0.50) or 0.0,
                points=points,
            )
        )
    if len(series) < 2:
        return None
    return LifecycleComparison(horizon_hours=horizon, series=tuple(series))


def _adaptive_bucket_bounds(max_hours: float) -> list[tuple[float, float]]:
    bounds = []
    start = 0.0
    while start < max_hours:
        width = 1 if start < 24 else 6 if start < 72 else 24
        end = start + width
        bounds.append((start, end))
        start = end
    return bounds


def _percentile(values: Sequence[float], ratio: float) -> float | None:
    return _quantile_elapsed(sorted(values), ratio)


def analyze_threads(
    comments: Sequence[Mapping[str, object]],
    *,
    published_at: int,
    cutoff_at: int,
    complete_all_run: bool,
) -> ThreadAnalysis:
    roots = [row for row in comments if int(row["level"]) == 0]
    children = [row for row in comments if int(row["level"]) > 0]
    declared = sum(max(0, int(row["reply_count"])) for row in roots)
    actual = len(children)
    coverage = actual / declared if declared else None
    enabled = bool(complete_all_run and coverage is not None and coverage >= MIN_THREAD_COVERAGE)

    root_by_id = {int(row["rpid"]): row for row in roots}
    declared_counts = sorted(
        (max(0, int(row["reply_count"])) for row in roots), reverse=True
    )
    top_count = max(1, math.ceil(len(roots) * 0.10)) if roots else 0
    static_top_ten_share = (
        sum(declared_counts[:top_count]) / declared if declared else None
    )

    high_like_root_share = None
    high_like_child_share = None
    actual_top_ten_share = None
    migration_buckets: tuple[CountBucket, ...] = ()
    migration_point = None
    delay_median = None
    delay_p90 = None
    invalid_delays = 0
    if enabled:
        likes = sorted((max(0, int(row["like_count"])) for row in roots), reverse=True)
        high_like_ids: set[int] = set()
        if likes:
            threshold = likes[max(0, math.ceil(len(likes) * 0.20) - 1)]
            if threshold > 0:
                high_like_ids = {
                    int(row["rpid"])
                    for row in roots
                    if int(row["like_count"]) >= threshold
                }
        high_like_root_share = len(high_like_ids) / len(roots) if roots else None
        children_by_root = Counter(int(row["root_rpid"]) for row in children)
        high_like_child_share = (
            sum(children_by_root[root_id] for root_id in high_like_ids) / actual
            if actual and high_like_ids
            else None
        )
        actual_counts = sorted(children_by_root.values(), reverse=True)
        actual_top_count = max(1, math.ceil(len(roots) * 0.10)) if roots else 0
        actual_top_ten_share = (
            sum(actual_counts[:actual_top_count]) / actual if actual else None
        )

        valid_roots = []
        valid_children = []
        first_delay_by_root: dict[int, float] = {}
        for row in roots:
            value, reason = _elapsed_hours(row["ctime"], published_at, cutoff_at)
            if reason is None and value is not None:
                valid_roots.append(value)
        for row in children:
            value, reason = _elapsed_hours(row["ctime"], published_at, cutoff_at)
            if reason is None and value is not None:
                valid_children.append(value)
            root = root_by_id.get(int(row["root_rpid"]))
            if root is None:
                invalid_delays += 1
                continue
            delay = int(row["ctime"]) - int(root["ctime"])
            if delay < 0:
                invalid_delays += 1
            else:
                root_id = int(root["rpid"])
                delay_hours = delay / HOUR
                current = first_delay_by_root.get(root_id)
                if current is None or delay_hours < current:
                    first_delay_by_root[root_id] = delay_hours
        max_event = max(valid_roots + valid_children, default=0.0)
        max_hours = min((cutoff_at - published_at) / HOUR, max_event + 24)
        buckets = []
        for start, end in _adaptive_bucket_bounds(max_hours):
            buckets.append(
                CountBucket(
                    start_hours=start,
                    end_hours=end,
                    root_count=sum(start <= value < end for value in valid_roots),
                    child_count=sum(start <= value < end for value in valid_children),
                    complete=end <= (cutoff_at - published_at) / HOUR,
                )
            )
        migration_buckets = tuple(buckets)
        qualifies = [
            bucket.complete
            and bucket.total_count >= MIN_MIGRATION_ACTIONS
            and bucket.child_count / bucket.width_hours
            > bucket.root_count / bucket.width_hours
            for bucket in migration_buckets
        ]
        for index in range(len(qualifies) - 1):
            if qualifies[index] and qualifies[index + 1]:
                migration_point = migration_buckets[index].start_hours
                break
        delays = sorted(first_delay_by_root.values())
        if delays:
            delay_median = median(delays)
            delay_p90 = _percentile(delays, 0.90)

    return ThreadAnalysis(
        declared_children=declared,
        actual_children=actual,
        coverage=coverage,
        complete_all_run=complete_all_run,
        time_analysis_enabled=enabled,
        static_top_ten_share=static_top_ten_share,
        high_like_root_share=high_like_root_share,
        high_like_child_share=high_like_child_share,
        actual_top_ten_share=actual_top_ten_share,
        migration_buckets=migration_buckets,
        migration_point_hours=migration_point,
        reply_delay_median_hours=delay_median,
        reply_delay_p90_hours=delay_p90,
        invalid_reply_delays=invalid_delays,
    )
