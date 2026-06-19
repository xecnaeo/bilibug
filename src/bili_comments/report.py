from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .database import Database
from .errors import ConfigurationError
from .lifecycle import (
    CountBucket,
    LifecycleAnalysis,
    LifecycleComparison,
    ThreadAnalysis,
    analyze_lifecycle,
    analyze_threads,
    compare_lifecycles,
)

METRICS = (
    ("view_count", "播放"),
    ("like_count", "点赞"),
    ("favorite_count", "收藏"),
    ("coin_count", "投币"),
    ("share_count", "分享"),
    ("reply_count", "评论"),
)


def _number(value: object) -> str:
    return f"{int(value or 0):,}"


def _percent(part: int, whole: int) -> str:
    return "—" if whole <= 0 else f"{part / whole:.1%}"


def _timestamp(value: object) -> str:
    if not value:
        return "—"
    text = str(value)
    try:
        return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def _duration_hours(first: object, last: object) -> float:
    try:
        start = datetime.fromisoformat(str(first))
        end = datetime.fromisoformat(str(last))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return max(0.0, (end - start).total_seconds() / 3600)
    except ValueError:
        return 0.0


def _as_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _trend_window(observations: Sequence[Mapping[str, object]], days: int) -> list[Mapping[str, object]]:
    dated = [
        (parsed, row)
        for row in observations
        if (parsed := _as_datetime(row["observed_at"])) is not None
    ]
    if not dated:
        return []
    threshold = dated[-1][0] - timedelta(days=days)
    return [row for observed_at, row in dated if observed_at >= threshold]


def _gap_count(observations: Sequence[Mapping[str, object]]) -> int:
    dates = [_as_datetime(row["observed_at"]) for row in observations]
    valid = [value for value in dates if value is not None]
    return sum(
        (current - previous).total_seconds() > 36 * 60 * 60
        for previous, current in zip(valid, valid[1:])
    )


def _duration_label(hours: float | None) -> str:
    if hours is None:
        return "—"
    if hours < 1:
        return f"{hours * 60:.0f} 分钟"
    if hours < 48:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


def _ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _lifecycle_confidence(
    *, complete_root_run: bool, platform_replies: int | None, expected_replies: int
) -> str:
    if not complete_root_run:
        return "低"
    if platform_replies is not None and platform_replies == expected_replies:
        return "高"
    return "中"


def _bar_chart(title: str, values: Sequence[tuple[str, int]]) -> str:
    if not values:
        return f'<section class="chart"><h4>{escape(title)}</h4><p class="muted">无数据</p></section>'
    width, label_width, bar_width = 660, 125, 430
    row_height = 31
    height = 42 + row_height * len(values)
    maximum = max((value for _, value in values), default=0) or 1
    rows = []
    for index, (label, value) in enumerate(values):
        y = 31 + index * row_height
        length = round(bar_width * value / maximum)
        rows.append(
            f'<text x="0" y="{y + 15}" class="svg-label">{escape(label)}</text>'
            f'<rect x="{label_width}" y="{y}" width="{length}" height="20" rx="4" />'
            f'<text x="{label_width + length + 8}" y="{y + 15}" class="svg-value">{_number(value)}</text>'
        )
    return (
        f'<section class="chart"><h4>{escape(title)}</h4>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        + "".join(rows)
        + "</svg></section>"
    )


def _line_chart(
    title: str, observations: Sequence[Mapping[str, object]], field: str
) -> str:
    points = [
        (parsed, int(row[field]))
        for row in observations
        if (parsed := _as_datetime(row["observed_at"])) is not None
    ]
    if not points:
        return f'<section class="chart"><h4>{escape(title)}</h4><p class="muted">无近期观测</p></section>'
    width, height, padding = 660, 220, 35
    first_time, last_time = points[0][0], points[-1][0]
    total_seconds = max(1.0, (last_time - first_time).total_seconds())
    values = [value for _, value in points]
    minimum, maximum = min(values), max(values)
    value_range = max(1, maximum - minimum)

    coordinates = [
        (
            padding
            + (width - padding * 2)
            * (observed_at - first_time).total_seconds()
            / total_seconds,
            height
            - padding
            - (height - padding * 2) * (value - minimum) / value_range,
            observed_at,
            value,
        )
        for observed_at, value in points
    ]
    segments: list[list[tuple[float, float, datetime, int]]] = [[]]
    for point in coordinates:
        if (
            segments[-1]
            and (point[2] - segments[-1][-1][2]).total_seconds() > 36 * 60 * 60
        ):
            segments.append([])
        segments[-1].append(point)
    lines = "".join(
        f'<polyline class="trend-line" points="'
        + " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in segment)
        + '" />'
        for segment in segments
        if len(segment) > 1
    )
    circles = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4"><title>{escape(_timestamp(observed_at.isoformat()))}: {_number(value)}</title></circle>'
        for x, y, observed_at, value in coordinates
    )
    gaps = len(segments) - 1
    gap_text = f" · 数据缺口 {gaps}" if gaps else ""
    return (
        f'<section class="chart"><h4>{escape(title)}</h4>'
        f'<p class="muted compact">{len(points)} 个点{gap_text}</p>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        f'<text x="0" y="16" class="svg-value">最大 {_number(maximum)}</text>'
        f'<text x="0" y="{height - 5}" class="svg-value">最小 {_number(minimum)}</text>'
        f'{lines}{circles}</svg></section>'
    )


def _bucket_chart(title: str, buckets: Sequence[CountBucket], *, unit: str) -> str:
    if not buckets:
        return f'<section class="chart"><h4>{escape(title)}</h4><p class="muted">无可用时间桶</p></section>'
    width, height = 720, 240
    padding_left, padding_right, padding_top, padding_bottom = 40, 12, 18, 36
    chart_width = width - padding_left - padding_right
    chart_height = height - padding_top - padding_bottom
    maximum = max((bucket.root_count for bucket in buckets), default=0) or 1
    slot = chart_width / len(buckets)
    bars = []
    for index, bucket in enumerate(buckets):
        bar_height = chart_height * bucket.root_count / maximum
        x = padding_left + index * slot + 1
        y = padding_top + chart_height - bar_height
        css = "lifecycle-bar" if bucket.complete else "lifecycle-bar partial"
        bars.append(
            f'<rect class="{css}" x="{x:.1f}" y="{y:.1f}" width="{max(1.0, slot - 2):.1f}" height="{bar_height:.1f}">'
            f'<title>{bucket.start_hours:g}–{bucket.end_hours:g} 小时：{bucket.root_count} 条</title></rect>'
        )
    label_step = max(1, len(buckets) // 6)
    labels = "".join(
        f'<text x="{padding_left + index * slot:.1f}" y="{height - 8}" class="svg-value">{escape(str(index if unit == "小时" else index + 1))}{escape(unit)}</text>'
        for index in range(0, len(buckets), label_step)
    )
    return (
        f'<section class="chart wide"><h4>{escape(title)}</h4>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        f'<text x="0" y="16" class="svg-value">峰值 {_number(maximum)}</text>'
        + "".join(bars)
        + labels
        + "</svg></section>"
    )


def _percentage_line_chart(
    title: str, points: Sequence[tuple[float, float]], *, x_suffix: str = "小时"
) -> str:
    if not points:
        return f'<section class="chart"><h4>{escape(title)}</h4><p class="muted">无可用数据</p></section>'
    width, height, padding = 720, 240, 35
    maximum_x = max(point[0] for point in points) or 1
    coordinates = [
        (
            padding + (width - padding * 2) * x / maximum_x,
            height - padding - (height - padding * 2) * value,
        )
        for x, value in points
    ]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in coordinates)
    return (
        f'<section class="chart wide"><h4>{escape(title)}</h4>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        f'<text x="0" y="16" class="svg-value">100%</text>'
        f'<text x="0" y="{height - 5}" class="svg-value">0%</text>'
        f'<polyline class="trend-line" points="{line}" />'
        f'<text x="{width - 90}" y="{height - 5}" class="svg-value">{maximum_x:g} {escape(x_suffix)}</text>'
        "</svg></section>"
    )


def _comparison_chart(comparison: LifecycleComparison) -> str:
    palette = ("#3d65d8", "#d85b3d", "#16856b", "#8b4fc1", "#c08a12")
    width, height, padding = 760, 280, 40
    lines = []
    legend = []
    for index, series in enumerate(comparison.series):
        color = palette[index % len(palette)]
        coordinates = " ".join(
            f"{padding + (width - padding * 2) * x / comparison.horizon_hours:.1f},"
            f"{height - padding - (height - padding * 2) * value:.1f}"
            for x, value in series.points
        )
        lines.append(
            f'<polyline points="{coordinates}" style="fill:none;stroke:{color};stroke-width:3" />'
        )
        legend.append(
            f'<span><i style="background:{color}"></i>{escape(series.bvid)}</span>'
        )
    return (
        '<section class="comparison"><h2>多视频生命周期比较</h2>'
        f'<p class="muted">共同成熟窗口为发布后 {comparison.horizon_hours} 小时；每条曲线以该窗口内评论总数为100%。</p>'
        f'<div class="legend">{"".join(legend)}</div>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="多视频累计评论比例">'
        f'<text x="0" y="16" class="svg-value">100%</text>'
        f'<text x="0" y="{height - 5}" class="svg-value">0%</text>'
        + "".join(lines)
        + "</svg>"
        + _table(
            ("视频", "共同窗口评论数", "达到50%"),
            (
                (item.bvid, _number(item.comment_count), _duration_label(item.t50_hours))
                for item in comparison.series
            ),
        )
        + "</section>"
    )


def _content_section(database: Database, bvid: str) -> str:
    from .content import MIN_DOCUMENT_FREQUENCY, analyze_video

    analysis = analyze_video(database, bvid)
    frequency_chart = _bar_chart(
        "高频关键词", [(item.token, item.count) for item in analysis.frequencies]
    )
    tfidf_rows = (
        (
            item.token,
            item.count,
            item.document_frequency,
            f"{item.score:.4f}",
        )
        for item in analysis.tfidf
    )
    pair_rows = (
        (item.left, item.right, item.count) for item in analysis.cooccurrences
    )
    empty_note = (
        '<p class="warnings">没有词项达到最低文档频率，未生成关键词。</p>'
        if not analysis.frequencies
        else ""
    )
    return f"""
      <h3>一级评论内容分析</h3>
      <p class="muted">仅分析一级评论的聚合词项，不展示原文或完整单词评论。分词器 jieba {escape(analysis.analyzer_version)}；分析 {_number(analysis.document_count)} 条评论；最低文档频率 {MIN_DOCUMENT_FREQUENCY}。</p>
      {empty_note}
      <div class="chart-grid">
        {frequency_chart}
        <section class="chart"><h4>TF-IDF 关键词</h4>{_table(('词项', '次数', '评论数', '得分'), tfidf_rows)}</section>
      </div>
      <h4 class="subheading">关键词共现</h4>
      {_table(('词项 A', '词项 B', '共同出现的评论数'), pair_rows)}
    """


def _table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in row) + "</tr>"
        )
    return (
        "<div class=\"table-wrap\"><table><thead><tr>"
        + "".join(f"<th>{escape(header)}</th>" for header in headers)
        + "</tr></thead><tbody>"
        + ("".join(body) if body else f'<tr><td colspan="{len(headers)}">无数据</td></tr>')
        + "</tbody></table></div>"
    )


def _bucket(value: int, limits: Sequence[tuple[int, str]], fallback: str) -> str:
    for upper, label in limits:
        if value <= upper:
            return label
    return fallback


def _comment_rows(database: Database, bvid: str) -> list[Mapping[str, object]]:
    return list(
        database.connection.execute(
            """
            SELECT rpid, ctime, like_count, reply_count, root_rpid, parent_rpid,
                   level, pin_type, state
            FROM comments WHERE bvid = ? ORDER BY level, rpid
            """,
            (bvid,),
        )
    )


def _cutoff_at(
    database: Database,
    video: Mapping[str, object],
    observations: Sequence[Mapping[str, object]],
) -> int:
    candidates = [_as_datetime(video["last_crawled_at"])]
    candidates.extend(_as_datetime(row["observed_at"]) for row in observations)
    candidates.extend(
        _as_datetime(row["value"])
        for row in database.connection.execute(
            """
            SELECT COALESCE(completed_at, checkpoint_updated_at, started_at) AS value
            FROM crawl_runs WHERE bvid = ?
            """,
            (video["bvid"],),
        )
    )
    valid = [value for value in candidates if value is not None]
    return int(max(valid).timestamp()) if valid else 0


def _has_complete_run(database: Database, bvid: str, replies_mode: str) -> bool:
    return (
        database.connection.execute(
            """
            SELECT 1 FROM crawl_runs
            WHERE bvid = ? AND replies_mode = ? AND status = 'completed'
              AND completeness = 'complete' AND root_finished = 1
            LIMIT 1
            """,
            (bvid, replies_mode),
        ).fetchone()
        is not None
    )


def _lifecycle_section(
    analysis: LifecycleAnalysis | None, *, confidence: str
) -> str:
    if analysis is None:
        return """
      <h3>一级评论生命周期</h3>
      <p class="warnings">缺少有效的视频发布时间或最近采集时间，无法计算生命周期。</p>
        """
    invalid_total = (
        analysis.invalid_before_publish
        + analysis.invalid_after_cutoff
        + analysis.invalid_timestamp
    )
    invalid_note = (
        f'<p class="warnings">已排除 {invalid_total} 条时间异常的一级评论：'
        f'发布前 {analysis.invalid_before_publish}，采集时间后 {analysis.invalid_after_cutoff}，'
        f'无效时间 {analysis.invalid_timestamp}。</p>'
        if invalid_total
        else ""
    )
    peak = (
        f"发布后第 {analysis.peak_hour}–{analysis.peak_hour + 1} 小时，{analysis.peak_count} 条"
        if analysis.peak_hour is not None
        else "—"
    )
    return f"""
      <h3>一级评论生命周期</h3>
      <p class="muted">以视频发布时间为零点；可信度：<strong>{escape(confidence)}</strong>。比例均基于当前已采集的 {_number(analysis.root_count)} 条有效一级评论。</p>
      {invalid_note}
      <div class="cards metrics-six">
        <div class="card"><span>评论峰值</span><strong class="small-value">{escape(peak)}</strong></div>
        <div class="card"><span>T50</span><strong>{_duration_label(analysis.t50_hours)}</strong></div>
        <div class="card"><span>T80</span><strong>{_duration_label(analysis.t80_hours)}</strong></div>
        <div class="card"><span>T90</span><strong>{_duration_label(analysis.t90_hours)}</strong></div>
        <div class="card"><span>首周占比</span><strong>{_ratio(analysis.first_week_share)}</strong></div>
        <div class="card"><span>7天后长尾</span><strong>{_ratio(analysis.long_tail_share)}</strong></div>
      </div>
      {_table(('阶段', '当前评论占比'), (
          ('发布后24小时', _ratio(analysis.first_day_share)),
          ('发布后3天', _ratio(analysis.first_three_days_share)),
          ('发布后7天', _ratio(analysis.first_week_share)),
      ))}
      <div class="chart-grid lifecycle-grid">
        {_bucket_chart('发布后前24小时 · 每小时新增一级评论', analysis.hourly_counts, unit='小时')}
        {_bucket_chart('发布后前30天 · 每日新增一级评论', analysis.daily_counts, unit='天')}
        {_percentage_line_chart('发布后前7天 · 累计一级评论占当前总量比例', analysis.cumulative_7d)}
      </div>
    """


def _thread_rate_chart(buckets: Sequence[CountBucket]) -> str:
    if not buckets:
        return '<section class="chart"><h4>新增观点与子评论速率</h4><p class="muted">无可用时间桶</p></section>'
    width, height, padding = 720, 240, 35
    root_rates = [bucket.root_count / bucket.width_hours for bucket in buckets]
    child_rates = [bucket.child_count / bucket.width_hours for bucket in buckets]
    maximum = max(root_rates + child_rates) or 1

    def points(values: Sequence[float]) -> str:
        denominator = max(1, len(values) - 1)
        return " ".join(
            f"{padding + (width - padding * 2) * index / denominator:.1f},"
            f"{height - padding - (height - padding * 2) * value / maximum:.1f}"
            for index, value in enumerate(values)
        )

    return (
        '<section class="chart wide"><h4>新增一级评论与子评论速率</h4>'
        '<div class="legend"><span><i style="background:#3d65d8"></i>一级评论/小时</span>'
        '<span><i style="background:#d85b3d"></i>子评论/小时</span></div>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="讨论迁移速率">'
        f'<text x="0" y="16" class="svg-value">峰值 {maximum:.1f}/小时</text>'
        f'<polyline points="{points(root_rates)}" style="fill:none;stroke:#3d65d8;stroke-width:3" />'
        f'<polyline points="{points(child_rates)}" style="fill:none;stroke:#d85b3d;stroke-width:3" />'
        "</svg></section>"
    )


def _thread_section(analysis: ThreadAnalysis | None) -> str:
    if analysis is None:
        return """
      <h3>讨论迁移与线程集中度</h3>
      <p class="warnings">缺少有效生命周期时间，无法分析讨论迁移。</p>
        """
    coverage = _ratio(analysis.coverage)
    static_rows = (
        ("一级评论声明子回复", _number(analysis.declared_children)),
        ("已采集子评论", _number(analysis.actual_children)),
        ("楼中楼覆盖率", coverage),
        ("声明回复最多的前10%线程占比", _ratio(analysis.static_top_ten_share)),
    )
    disclaimer = (
        "点赞数仅作为高互动代理。结果表示相关性，可能受到发布时间、热门排序、置顶和曝光时长影响。"
    )
    if not analysis.time_analysis_enabled:
        reasons = []
        if not analysis.complete_all_run:
            reasons.append("没有完整的 all 运行")
        if analysis.coverage is None or analysis.coverage < 0.90:
            reasons.append("楼中楼覆盖率不足90%")
        reason = "；".join(reasons) or "分析门槛未满足"
        return f"""
      <h3>讨论迁移与线程集中度</h3>
      {_table(('指标', '结果'), static_rows)}
      <p class="warnings">讨论迁移结论未生成：{escape(reason)}。不绘制子评论时间曲线，也不计算迁移点或回复延迟。</p>
      <p class="muted">{escape(disclaimer)}</p>
        """
    migration = (
        _duration_label(analysis.migration_point_hours)
        if analysis.migration_point_hours is not None
        else "未检测到符合门槛的迁移点"
    )
    share_points = tuple(
        (
            bucket.end_hours,
            bucket.child_count / bucket.total_count if bucket.total_count else 0.0,
        )
        for bucket in analysis.migration_buckets
    )
    return f"""
      <h3>讨论迁移与线程集中度</h3>
      {_table(('指标', '结果'), (*static_rows,
          ('讨论迁移点', migration),
          ('首次回复延迟中位数', _duration_label(analysis.reply_delay_median_hours)),
          ('首次回复延迟P90', _duration_label(analysis.reply_delay_p90_hours)),
          ('高互动一级评论占比', _ratio(analysis.high_like_root_share)),
          ('高互动一级评论承载子评论占比', _ratio(analysis.high_like_child_share)),
          ('实际子评论最多的前10%线程占比', _ratio(analysis.actual_top_ten_share)),
          ('无效回复延迟记录', _number(analysis.invalid_reply_delays)),
      ))}
      <div class="chart-grid lifecycle-grid">
        {_thread_rate_chart(analysis.migration_buckets)}
        {_percentage_line_chart('子评论占全部讨论行为比例', share_points)}
      </div>
      <p class="muted">{escape(disclaimer)}</p>
    """


def _video_section(
    database: Database,
    video: Mapping[str, object],
    comments: Sequence[Mapping[str, object]],
    observations: Sequence[Mapping[str, object]],
    lifecycle: LifecycleAnalysis | None,
    threads: ThreadAnalysis | None,
    *,
    complete_root_run: bool,
    days: int,
    content_analysis: bool,
) -> tuple[str, dict[str, int]]:
    bvid = str(video["bvid"])
    roots = [row for row in comments if int(row["level"]) == 0]
    children = [row for row in comments if int(row["level"]) > 0]
    declared_children = sum(int(row["reply_count"]) for row in roots)
    children_by_root = Counter(int(row["root_rpid"]) for row in children)
    roots_with_replies = [row for row in roots if int(row["reply_count"]) > 0]
    fully_covered = sum(
        children_by_root[int(row["rpid"])] >= int(row["reply_count"])
        for row in roots_with_replies
    )
    partially_covered = sum(
        0 < children_by_root[int(row["rpid"])] < int(row["reply_count"])
        for row in roots_with_replies
    )
    untouched = len(roots_with_replies) - fully_covered - partially_covered

    window_observations = _trend_window(observations, days)
    first_observation = window_observations[0] if window_observations else None
    latest_observation = observations[-1] if observations else None
    span_hours = (
        _duration_hours(
            window_observations[0]["observed_at"],
            window_observations[-1]["observed_at"],
        )
        if window_observations
        else 0.0
    )
    gap_count = _gap_count(window_observations)
    enough_for_trend = len(window_observations) >= 3 and span_hours >= 24
    platform_replies = int(latest_observation["reply_count"]) if latest_observation else None
    expected_replies = len(roots) + declared_children
    confidence = _lifecycle_confidence(
        complete_root_run=complete_root_run,
        platform_replies=platform_replies,
        expected_replies=expected_replies,
    )

    runs = list(
        database.connection.execute(
            "SELECT * FROM crawl_runs WHERE bvid = ? ORDER BY id DESC", (bvid,)
        )
    )
    latest_run = runs[0] if runs else None
    failed_runs = [
        row
        for row in runs
        if str(row["status"]) != "completed" or str(row["completeness"]) != "complete"
    ]
    warnings = []
    if not str(video["category_name"] or ""):
        warnings.append("missing_category_name：分区名称缺失，仅展示分区 ID")
    if declared_children and len(children) < declared_children:
        warnings.append(
            f"楼中楼不完整：已采集 {_number(len(children))}/{_number(declared_children)} 条"
        )
    if platform_replies is not None and expected_replies != platform_replies:
        warnings.append(
            f"一级评论完整性待核验：本地推算 {_number(expected_replies)}，平台 {_number(platform_replies)}"
        )

    metric_rows = []
    for field, label in METRICS:
        first = int(first_observation[field]) if first_observation else 0
        latest = int(latest_observation[field]) if latest_observation else 0
        delta = latest - first if first_observation else 0
        metric_rows.append(
            (label, _number(first) if first_observation else "—", _number(latest), f"{delta:+,}" if first_observation else "—")
        )

    trend_charts = "".join(
        _line_chart(f"{label} · 最近 {days} 天", window_observations, field)
        for field, label in METRICS
    )
    content_html = _content_section(database, bvid) if content_analysis else ""
    like_counts: Counter[str] = Counter()
    reply_counts: Counter[str] = Counter()
    for row in roots:
        like_counts[_bucket(int(row["like_count"]), ((0, "0"), (9, "1–9"), (99, "10–99")), "100+")] += 1
        reply_counts[_bucket(int(row["reply_count"]), ((0, "0"), (9, "1–9")), "10+")] += 1

    run_rows = []
    for row in failed_runs:
        if not bool(row["root_finished"]):
            checkpoint = "一级评论游标"
        elif row["sub_root_rpid"] is not None:
            checkpoint = f"根评论 {row['sub_root_rpid']} / 第 {row['sub_page']} 页"
        else:
            checkpoint = "—"
        run_rows.append(
            (
                row["id"],
                row["comment_order"],
                row["replies_mode"],
                row["status"],
                row["completeness"],
                checkpoint,
                row["end_reason"] or "—",
                row["error"] or "—",
            )
        )
    category = str(video["category_name"] or "") or f"ID {video['category_id']}"
    trend_label = "可用于趋势观察" if enough_for_trend else "样本不足，不能判断趋势"
    if not window_observations:
        trend_note = f"最近 {days} 天没有可用观测，不能判断趋势。"
    else:
        trend_note = (
            f"{trend_label}；最近 {days} 天有 {len(window_observations)} 个观测点，"
            f"跨度 {span_hours:.1f} 小时。"
        )
    if gap_count:
        trend_note += f"存在 {gap_count} 个超过 36 小时的数据缺口，折线不做插值。"
    completeness = (
        f"{latest_run['status']} / {latest_run['completeness']}"
        if latest_run is not None
        else "无运行记录"
    )
    warning_html = "".join(f"<li>{escape(item)}</li>" for item in warnings)
    section = f"""
    <section class="video" id="{escape(bvid)}">
      <header class="video-header">
        <div><p class="eyebrow">{escape(bvid)}</p><h2>{escape(str(video['title']))}</h2></div>
        <span class="status">{escape(completeness)}</span>
      </header>
      <p class="meta">分区：{escape(category)} · 发布时间：{_timestamp(datetime.fromtimestamp(int(video['published_at']), timezone.utc).isoformat()) if int(video['published_at']) else '—'} · 最近抓取：{_timestamp(video['last_crawled_at'])}</p>
      <div class="cards">
        <div class="card"><span>一级评论</span><strong>{_number(len(roots))}</strong></div>
        <div class="card"><span>声明子回复</span><strong>{_number(declared_children)}</strong></div>
        <div class="card"><span>已采集子回复</span><strong>{_number(len(children))}</strong></div>
        <div class="card"><span>楼中楼覆盖</span><strong>{_percent(len(children), declared_children)}</strong></div>
      </div>
      <h3>数据质量</h3>
      <ul class="warnings">{warning_html or '<li class="ok">未发现明显数据质量问题</li>'}</ul>
      {_table(('指标', '结果'), (
          ('平台评论数', _number(platform_replies) if platform_replies is not None else '—'),
          ('本地一级评论 + 声明子回复', _number(expected_replies)),
          ('有回复的一级评论', _number(len(roots_with_replies))),
          ('楼中楼完整 / 部分 / 未采集', f'{fully_covered} / {partially_covered} / {untouched}'),
          ('当前抓取状态', completeness),
          ('生命周期可信度', confidence),
      ))}
      {_lifecycle_section(lifecycle, confidence=confidence)}
      {_thread_section(threads)}
      {content_html}
      <h3>当前一级评论互动分布</h3>
      <div class="chart-grid">
        {_bar_chart('一级评论点赞分布', [(key, like_counts[key]) for key in ('0', '1–9', '10–99', '100+')])}
        {_bar_chart('一级评论回复数分布', [(key, reply_counts[key]) for key in ('0', '1–9', '10+')])}
      </div>
      <details class="auxiliary">
        <summary>辅助：采集点互动趋势（最近 {days} 天）</summary>
        <p class="muted">{escape(trend_note)}差值仅表示窗口内首个观测与最新观测的差异，不代表视频发布以来的完整历史。</p>
        {_table(('指标', '首次', '最近', '差值'), metric_rows)}
        <div class="chart-grid">{trend_charts}</div>
      </details>
      <h3>异常运行</h3>
      {_table(('运行', '排序', '范围', '状态', '完整性', '断点', '结束原因', '错误'), run_rows)}
    </section>
    """
    return section, {
        "roots": len(roots),
        "children": len(children),
        "runs": len(runs),
        "failed_runs": len(failed_runs),
    }


def generate_report(
    database: Database,
    bvids: Sequence[str],
    output: str | Path,
    *,
    content_analysis: bool = False,
    days: int = 7,
) -> int:
    if days <= 0:
        raise ConfigurationError("趋势窗口天数必须大于 0")
    placeholders = ",".join("?" for _ in bvids)
    videos = list(
        database.connection.execute(
            f"SELECT * FROM videos WHERE bvid IN ({placeholders}) ORDER BY bvid",
            tuple(bvids),
        )
    )
    report_items = []
    comparison_inputs = []
    for video in videos:
        bvid = str(video["bvid"])
        comments = _comment_rows(database, bvid)
        observations = list(database.iter_video_observations(bvid))
        cutoff = _cutoff_at(database, video, observations)
        published_at = int(video["published_at"])
        lifecycle = analyze_lifecycle(
            comments, published_at=published_at, cutoff_at=cutoff
        )
        complete_all_run = _has_complete_run(database, bvid, "all")
        threads = (
            analyze_threads(
                comments,
                published_at=published_at,
                cutoff_at=cutoff,
                complete_all_run=complete_all_run,
            )
            if lifecycle is not None
            else None
        )
        complete_root_run = complete_all_run or _has_complete_run(database, bvid, "root")
        report_items.append(
            (
                video,
                comments,
                observations,
                lifecycle,
                threads,
                complete_root_run,
            )
        )
        if lifecycle is not None:
            comparison_inputs.append((bvid, lifecycle))
    comparison = compare_lifecycles(comparison_inputs)
    comparison_html = (
        _comparison_chart(comparison)
        if comparison is not None
        else '<section class="comparison"><h2>多视频生命周期比较</h2><p class="muted">有效视频不足、共同成熟窗口不足6小时，或共同窗口内没有评论，暂不生成归一化比较。</p></section>'
    )
    sections = []
    totals = defaultdict(int)
    for (
        video,
        comments,
        observations,
        lifecycle,
        threads,
        complete_root_run,
    ) in report_items:
        section, counts = _video_section(
            database,
            video,
            comments,
            observations,
            lifecycle,
            threads,
            complete_root_run=complete_root_run,
            days=days,
            content_analysis=content_analysis,
        )
        sections.append(section)
        for key, value in counts.items():
            totals[key] += value

    batches = list(database.connection.execute("SELECT * FROM batch_runs"))
    succeeded_batches = sum(str(row["status"]) == "completed" for row in batches)
    batch_rate = _percent(succeeded_batches, len(batches))
    generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    navigation = "".join(
        f'<a href="#{escape(str(video["bvid"]))}">{escape(str(video["bvid"]))}</a>'
        for video in videos
    )
    html = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>B站采集数据离线报告</title>
<style>
:root{{--bg:#f5f7fb;--surface:#fff;--text:#172033;--muted:#667085;--line:#e5eaf1;--accent:#3d65d8;--accent-soft:#edf2ff;--warn:#9a6700;--ok:#147a4b}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.6 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}}
main{{max-width:1180px;margin:auto;padding:40px 24px 80px}} h1{{font-size:34px;margin:4px 0}} h2{{font-size:25px;margin:0}} h3{{margin:30px 0 10px}} h4{{margin:0 0 12px}}
.eyebrow{{margin:0;color:var(--accent);font-weight:700;letter-spacing:.04em}} .muted,.meta{{color:var(--muted)}} .hero,.video{{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:28px;box-shadow:0 8px 28px rgba(23,32,51,.05)}}
.hero{{margin-bottom:22px}} .comparison{{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:28px;margin-bottom:22px}} nav{{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}} nav a{{color:var(--accent);background:var(--accent-soft);padding:5px 10px;border-radius:8px;text-decoration:none}}
.video{{margin-top:22px}} .video-header{{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}} .status{{white-space:nowrap;background:var(--accent-soft);color:var(--accent);padding:6px 10px;border-radius:999px;font-size:13px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:22px 0}} .metrics-six{{grid-template-columns:repeat(3,1fr)}} .card{{border:1px solid var(--line);border-radius:12px;padding:15px}} .card span{{display:block;color:var(--muted)}} .card strong{{font-size:24px}} .card .small-value{{font-size:16px}}
.warnings{{padding-left:22px;color:var(--warn)}} .warnings .ok{{color:var(--ok)}} .table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:12px}} table{{width:100%;border-collapse:collapse;min-width:620px}} th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}} th{{background:#f9fafc;font-size:13px}} tr:last-child td{{border-bottom:0}}
.chart-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:18px}} .lifecycle-grid .wide{{grid-column:1/-1}} .chart{{border:1px solid var(--line);border-radius:12px;padding:16px;overflow:hidden}} .chart .table-wrap{{border:0}} .subheading{{margin-top:20px}} .compact{{margin:-8px 0 4px}} svg{{width:100%;height:auto}} svg rect{{fill:var(--accent)}} svg .lifecycle-bar.partial{{opacity:.35}} .trend-line{{fill:none;stroke:var(--accent);stroke-width:3;stroke-linecap:round;stroke-linejoin:round}} svg circle{{fill:var(--surface);stroke:var(--accent);stroke-width:3}} .svg-label,.svg-value{{font-size:13px;fill:var(--text)}} .legend{{display:flex;gap:18px;flex-wrap:wrap;margin:8px 0}} .legend span{{display:flex;align-items:center;gap:6px}} .legend i{{display:inline-block;width:18px;height:4px;border-radius:4px}} details.auxiliary{{margin-top:30px;border:1px solid var(--line);border-radius:12px;padding:14px}} details.auxiliary summary{{cursor:pointer;font-weight:700;font-size:17px}} footer{{color:var(--muted);margin-top:28px;text-align:center}}
@media(max-width:760px){{main{{padding:20px 12px 50px}}.cards,.metrics-six{{grid-template-columns:repeat(2,1fr)}}.chart-grid{{grid-template-columns:1fr}}.video-header{{display:block}}.status{{display:inline-block;margin-top:10px}}}}
@media print{{body{{background:#fff}}main{{max-width:none;padding:0}}.hero,.video{{box-shadow:none;break-inside:avoid}}nav{{display:none}}}}
</style>
</head>
<body><main>
  <section class="hero">
    <p class="eyebrow">BILI COMMENTS · OFFLINE REPORT</p>
    <h1>评论生命周期分析报告</h1>
    <p class="muted">生成时间：{escape(generated_at)}。生命周期以视频发布时间为零点；采集点互动趋势作为最近 {days} 天的辅助信息。报告不包含评论正文或作者标识。</p>
    <div class="cards">
      <div class="card"><span>视频</span><strong>{_number(len(videos))}</strong></div>
      <div class="card"><span>一级评论</span><strong>{_number(totals['roots'])}</strong></div>
      <div class="card"><span>已采集子回复</span><strong>{_number(totals['children'])}</strong></div>
      <div class="card"><span>批次完全成功率</span><strong>{batch_rate}</strong></div>
    </div>
    <p class="muted">抓取运行 {_number(totals['runs'])} 次，其中异常或不完整 {_number(totals['failed_runs'])} 次；批次 {_number(len(batches))} 个。</p>
    <nav>{navigation}</nav>
  </section>
  {comparison_html}
  {''.join(sections)}
  <footer>由 bili-comments 0.6.0 生成 · 单文件离线报告</footer>
</main></body></html>
"""
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")
    return len(videos)
