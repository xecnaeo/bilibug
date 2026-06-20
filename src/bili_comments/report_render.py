from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from typing import Iterable, Mapping, Sequence

from .content import ContentAnalysis, MIN_DOCUMENT_FREQUENCY
from .lifecycle import CountBucket, LifecycleAnalysis, LifecycleComparison, ThreadAnalysis
from .topic_evolution import TopicEvolution


def number(value: object) -> str:
    return f"{int(value or 0):,}"


def percent(part: int, whole: int) -> str:
    return "—" if whole <= 0 else f"{part / whole:.1%}"


def timestamp(value: object) -> str:
    if not value:
        return "—"
    text = str(value)
    try:
        return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text


def duration_label(hours: float | None) -> str:
    if hours is None:
        return "—"
    if hours < 1:
        return f"{hours * 60:.0f} 分钟"
    if hours < 48:
        return f"{hours:.1f} 小时"
    return f"{hours / 24:.1f} 天"


def ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def table(headers: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    body = [
        "<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in row) + "</tr>"
        for row in rows
    ]
    return (
        '<div class="table-wrap"><table><thead><tr>'
        + "".join(f"<th>{escape(header)}</th>" for header in headers)
        + "</tr></thead><tbody>"
        + ("".join(body) if body else f'<tr><td colspan="{len(headers)}">无数据</td></tr>')
        + "</tbody></table></div>"
    )


def bar_chart(title: str, values: Sequence[tuple[str, int]]) -> str:
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
            f'<text x="{label_width + length + 8}" y="{y + 15}" class="svg-value">{number(value)}</text>'
        )
    return (
        f'<section class="chart"><h4>{escape(title)}</h4>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        + "".join(rows)
        + "</svg></section>"
    )


def _as_datetime(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def observation_line_chart(
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
            padding + (width - padding * 2) * (observed_at - first_time).total_seconds() / total_seconds,
            height - padding - (height - padding * 2) * (value - minimum) / value_range,
            observed_at,
            value,
        )
        for observed_at, value in points
    ]
    segments: list[list[tuple[float, float, datetime, int]]] = [[]]
    for point in coordinates:
        if segments[-1] and (point[2] - segments[-1][-1][2]).total_seconds() > 36 * 60 * 60:
            segments.append([])
        segments[-1].append(point)
    lines = "".join(
        '<polyline class="trend-line" points="'
        + " ".join(f"{x:.1f},{y:.1f}" for x, y, _, _ in segment)
        + '" />'
        for segment in segments
        if len(segment) > 1
    )
    circles = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4"><title>{escape(timestamp(observed_at.isoformat()))}: {number(value)}</title></circle>'
        for x, y, observed_at, value in coordinates
    )
    gaps = len(segments) - 1
    gap_text = f" · 数据缺口 {gaps}" if gaps else ""
    return (
        f'<section class="chart"><h4>{escape(title)}</h4>'
        f'<p class="muted compact">{len(points)} 个点{gap_text}</p>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        f'<text x="0" y="16" class="svg-value">最大 {number(maximum)}</text>'
        f'<text x="0" y="{height - 5}" class="svg-value">最小 {number(minimum)}</text>'
        f'{lines}{circles}</svg></section>'
    )


def bucket_chart(title: str, buckets: Sequence[CountBucket], *, unit: str) -> str:
    if not buckets:
        return f'<section class="chart"><h4>{escape(title)}</h4><p class="muted">无可用时间桶</p></section>'
    width, height = 720, 240
    left, right, top, bottom = 40, 12, 18, 36
    chart_width, chart_height = width - left - right, height - top - bottom
    maximum = max((bucket.root_count for bucket in buckets), default=0) or 1
    slot = chart_width / len(buckets)
    bars = []
    for index, bucket in enumerate(buckets):
        bar_height = chart_height * bucket.root_count / maximum
        x, y = left + index * slot + 1, top + chart_height - bar_height
        css = "lifecycle-bar" if bucket.complete else "lifecycle-bar partial"
        bars.append(
            f'<rect class="{css}" x="{x:.1f}" y="{y:.1f}" width="{max(1.0, slot - 2):.1f}" height="{bar_height:.1f}">'
            f'<title>{bucket.start_hours:g}–{bucket.end_hours:g} 小时：{bucket.root_count} 条</title></rect>'
        )
    label_step = max(1, len(buckets) // 6)
    labels = "".join(
        f'<text x="{left + index * slot:.1f}" y="{height - 8}" class="svg-value">{index if unit == "小时" else index + 1}{escape(unit)}</text>'
        for index in range(0, len(buckets), label_step)
    )
    return (
        f'<section class="chart wide"><h4>{escape(title)}</h4>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        f'<text x="0" y="16" class="svg-value">峰值 {number(maximum)}</text>'
        + "".join(bars)
        + labels
        + "</svg></section>"
    )


def percentage_line_chart(
    title: str, points: Sequence[tuple[float, float]], *, x_suffix: str = "小时"
) -> str:
    if not points:
        return f'<section class="chart"><h4>{escape(title)}</h4><p class="muted">无可用数据</p></section>'
    width, height, padding = 720, 240, 35
    maximum_x = max(point[0] for point in points) or 1
    line = " ".join(
        f"{padding + (width - padding * 2) * x / maximum_x:.1f},"
        f"{height - padding - (height - padding * 2) * value:.1f}"
        for x, value in points
    )
    return (
        f'<section class="chart wide"><h4>{escape(title)}</h4>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">'
        f'<text x="0" y="16" class="svg-value">100%</text>'
        f'<text x="0" y="{height - 5}" class="svg-value">0%</text>'
        f'<polyline class="trend-line" points="{line}" />'
        f'<text x="{width - 90}" y="{height - 5}" class="svg-value">{maximum_x:g} {escape(x_suffix)}</text>'
        "</svg></section>"
    )


def comparison_section(comparison: LifecycleComparison) -> str:
    palette = ("#3d65d8", "#d85b3d", "#16856b", "#8b4fc1", "#c08a12")
    width, height, padding = 760, 280, 40
    lines, legend = [], []
    for index, series in enumerate(comparison.series):
        color = palette[index % len(palette)]
        coordinates = " ".join(
            f"{padding + (width - padding * 2) * x / comparison.horizon_hours:.1f},"
            f"{height - padding - (height - padding * 2) * value:.1f}"
            for x, value in series.points
        )
        lines.append(f'<polyline points="{coordinates}" style="fill:none;stroke:{color};stroke-width:3" />')
        legend.append(f'<span><i style="background:{color}"></i>{escape(series.bvid)}</span>')
    return (
        '<section class="comparison"><h2>多视频生命周期比较</h2>'
        f'<p class="muted">共同成熟窗口为发布后 {comparison.horizon_hours} 小时；每条曲线以该窗口内评论总数为100%。</p>'
        f'<div class="legend">{"".join(legend)}</div>'
        f'<svg viewBox="0 0 {width} {height}" role="img" aria-label="多视频累计评论比例">'
        f'<text x="0" y="16" class="svg-value">100%</text><text x="0" y="{height - 5}" class="svg-value">0%</text>'
        + "".join(lines)
        + "</svg>"
        + table(
            ("视频", "共同窗口评论数", "达到50%"),
            ((item.bvid, number(item.comment_count), duration_label(item.t50_hours)) for item in comparison.series),
        )
        + "</section>"
    )


def lifecycle_section(analysis: LifecycleAnalysis | None, *, confidence: str) -> str:
    if analysis is None:
        return '<h3>一级评论生命周期</h3><p class="warnings">缺少有效的视频发布时间或最近采集时间，无法计算生命周期。</p>'
    invalid_total = analysis.invalid_before_publish + analysis.invalid_after_cutoff + analysis.invalid_timestamp
    invalid_note = (
        f'<p class="warnings">已排除 {invalid_total} 条时间异常的一级评论：发布前 {analysis.invalid_before_publish}，'
        f'采集时间后 {analysis.invalid_after_cutoff}，无效时间 {analysis.invalid_timestamp}。</p>'
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
      <p class="muted">以视频发布时间为零点；可信度：<strong>{escape(confidence)}</strong>。比例均基于当前已采集的 {number(analysis.root_count)} 条有效一级评论。</p>
      {invalid_note}
      <div class="cards metrics-six">
        <div class="card"><span>评论峰值</span><strong class="small-value">{escape(peak)}</strong></div>
        <div class="card"><span>T50</span><strong>{duration_label(analysis.t50_hours)}</strong></div>
        <div class="card"><span>T80</span><strong>{duration_label(analysis.t80_hours)}</strong></div>
        <div class="card"><span>T90</span><strong>{duration_label(analysis.t90_hours)}</strong></div>
        <div class="card"><span>首周占比</span><strong>{ratio(analysis.first_week_share)}</strong></div>
        <div class="card"><span>7天后长尾</span><strong>{ratio(analysis.long_tail_share)}</strong></div>
      </div>
      {table(('阶段', '当前评论占比'), (
          ('发布后24小时', ratio(analysis.first_day_share)),
          ('发布后3天', ratio(analysis.first_three_days_share)),
          ('发布后7天', ratio(analysis.first_week_share)),
      ))}
      <div class="chart-grid lifecycle-grid">
        {bucket_chart('发布后前24小时 · 每小时新增一级评论', analysis.hourly_counts, unit='小时')}
        {bucket_chart('发布后前30天 · 每日新增一级评论', analysis.daily_counts, unit='天')}
        {percentage_line_chart('发布后前7天 · 累计一级评论占当前总量比例', analysis.cumulative_7d)}
      </div>
    """


def _thread_rate_chart(buckets: Sequence[CountBucket]) -> str:
    if not buckets:
        return '<section class="chart"><h4>新增观点与子评论速率</h4><p class="muted">无可用时间桶</p></section>'
    width, height, padding = 720, 240, 35
    roots = [bucket.root_count / bucket.width_hours for bucket in buckets]
    children = [bucket.child_count / bucket.width_hours for bucket in buckets]
    maximum = max(roots + children) or 1

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
        f'<svg viewBox="0 0 {width} {height}"><text x="0" y="16" class="svg-value">峰值 {maximum:.1f}/小时</text>'
        f'<polyline points="{points(roots)}" style="fill:none;stroke:#3d65d8;stroke-width:3" />'
        f'<polyline points="{points(children)}" style="fill:none;stroke:#d85b3d;stroke-width:3" /></svg></section>'
    )


def thread_section(analysis: ThreadAnalysis | None) -> str:
    if analysis is None:
        return '<h3>讨论迁移与线程集中度</h3><p class="warnings">缺少有效生命周期时间，无法分析讨论迁移。</p>'
    static_rows = (
        ("一级评论声明子回复", number(analysis.declared_children)),
        ("已采集子评论", number(analysis.actual_children)),
        ("楼中楼覆盖率", ratio(analysis.coverage)),
        ("声明回复最多的前10%线程占比", ratio(analysis.static_top_ten_share)),
    )
    disclaimer = "点赞数仅作为高互动代理。结果表示相关性，可能受到发布时间、热门排序、置顶和曝光时长影响。"
    if not analysis.time_analysis_enabled:
        reasons = []
        if not analysis.complete_all_run:
            reasons.append("没有完整的 all 运行")
        if analysis.coverage is None or analysis.coverage < 0.90:
            reasons.append("楼中楼覆盖率不足90%")
        return f"""
      <h3>讨论迁移与线程集中度</h3>{table(('指标', '结果'), static_rows)}
      <p class="warnings">讨论迁移结论未生成：{escape('；'.join(reasons) or '分析门槛未满足')}。不绘制子评论时间曲线，也不计算迁移点或回复延迟。</p>
      <p class="muted">{escape(disclaimer)}</p>"""
    migration = duration_label(analysis.migration_point_hours) if analysis.migration_point_hours is not None else "未检测到符合门槛的迁移点"
    share_points = tuple(
        (bucket.end_hours, bucket.child_count / bucket.total_count if bucket.total_count else 0.0)
        for bucket in analysis.migration_buckets
    )
    return f"""
      <h3>讨论迁移与线程集中度</h3>
      {table(('指标', '结果'), (*static_rows,
          ('讨论迁移点', migration),
          ('首次回复延迟中位数', duration_label(analysis.reply_delay_median_hours)),
          ('首次回复延迟P90', duration_label(analysis.reply_delay_p90_hours)),
          ('高互动一级评论占比', ratio(analysis.high_like_root_share)),
          ('高互动一级评论承载子评论占比', ratio(analysis.high_like_child_share)),
          ('实际子评论最多的前10%线程占比', ratio(analysis.actual_top_ten_share)),
          ('无效回复延迟记录', number(analysis.invalid_reply_delays)),
      ))}
      <div class="chart-grid lifecycle-grid">{_thread_rate_chart(analysis.migration_buckets)}{percentage_line_chart('子评论占全部讨论行为比例', share_points)}</div>
      <p class="muted">{escape(disclaimer)}</p>"""


def topic_evolution_section(analysis: TopicEvolution | None) -> str:
    if analysis is None:
        return '<h3>生命周期阶段主题演化</h3><p class="warnings">缺少有效生命周期时间，无法分析阶段主题。</p>'
    status_names = {"complete": "完整", "ongoing": "持续积累中", "not_started": "尚未到达"}
    stage_rows = []
    for stage in analysis.stages:
        if stage.eligible:
            result = "、".join(item.token for item in stage.keywords) or "没有词项达到最低频率"
        elif stage.comment_count < 20:
            result = "样本不足（少于20条）"
        else:
            result = "阶段尚未完整经历"
        stage_rows.append(
            (
                stage.definition.label,
                status_names[stage.status],
                number(stage.comment_count),
                ratio(stage.comment_share),
                result,
            )
        )
    if not analysis.comparable:
        evolution_html = '<p class="warnings">少于两个合格阶段，仅展示阶段统计，不输出主题变化结论。</p>'
    else:
        headers = "".join(f"<th>{escape(stage.definition.label)}</th>" for stage in analysis.stages)
        rows = []
        for term in analysis.heatmap_terms:
            cells = "".join(
                f'<td class="heat" style="--weight:{weight:.3f}">{weight:.0%}</td>'
                for weight in term.weights
            )
            rows.append(f"<tr><td>{escape(term.token)}</td>{cells}</tr>")
        heatmap = (
            '<div class="table-wrap"><table class="heatmap"><thead><tr><th>词项</th>'
            + headers
            + "</tr></thead><tbody>"
            + ("".join(rows) if rows else '<tr><td colspan="6">无数据</td></tr>')
            + "</tbody></table></div>"
        )
        new_rows = (
            (stage.definition.label, "、".join(stage.new_terms) or "—")
            for stage in analysis.stages
            if stage.new_terms
        )
        evolution_html = (
            heatmap
            + "<h4 class=\"subheading\">阶段变化摘要</h4>"
            + table(("阶段", "新增高权重词项"), new_rows)
            + f'<p class="muted">持续主题：{escape("、".join(analysis.persistent_terms) or "无")}</p>'
        )
    invalid_note = f"；排除 {analysis.invalid_comment_count} 条时间异常评论" if analysis.invalid_comment_count else ""
    return f"""
      <h3>生命周期阶段主题演化</h3>
      <p class="muted">固定阶段比较，只描述聚合词项权重变化，不推断情绪、立场或事件原因。分词器 jieba {escape(analysis.analyzer_version)}{invalid_note}。</p>
      {table(('阶段', '成熟度', '评论数', '占比', 'TF-IDF前10'), stage_rows)}
      {evolution_html}
    """


def content_section(analysis: ContentAnalysis) -> str:
    frequency_chart = bar_chart("高频关键词", [(item.token, item.count) for item in analysis.frequencies])
    return f"""
      <h3>一级评论内容分析（整体）</h3>
      <p class="muted">仅分析一级评论聚合词项，不展示原文或完整单词评论。分词器 jieba {escape(analysis.analyzer_version)}；分析 {number(analysis.document_count)} 条评论；最低文档频率 {MIN_DOCUMENT_FREQUENCY}。</p>
      <div class="chart-grid">{frequency_chart}
        <section class="chart"><h4>TF-IDF 关键词</h4>{table(('词项', '次数', '评论数', '得分'), ((item.token, item.count, item.document_frequency, f'{item.score:.4f}') for item in analysis.tfidf))}</section>
      </div>
      <h4 class="subheading">关键词共现</h4>
      {table(('词项 A', '词项 B', '共同出现的评论数'), ((item.left, item.right, item.count) for item in analysis.cooccurrences))}
    """
