from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .database import Database
from .errors import ConfigurationError

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


def _video_section(
    database: Database,
    video: Mapping[str, object],
    *,
    days: int,
    content_analysis: bool,
) -> tuple[str, dict[str, int]]:
    bvid = str(video["bvid"])
    comments = list(
        database.connection.execute(
            """
            SELECT rpid, ctime, like_count, reply_count, root_rpid, level,
                   pin_type, state
            FROM comments WHERE bvid = ? ORDER BY level, rpid
            """,
            (bvid,),
        )
    )
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

    observations = list(database.iter_video_observations(bvid))
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
    platform_replies = int(latest_observation["reply_count"]) if latest_observation else 0
    expected_replies = len(roots) + declared_children

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
    if not window_observations:
        warnings.append(f"最近 {days} 天没有可用观测，不能判断趋势")
    elif not enough_for_trend:
        warnings.append(
            f"样本不足：最近 {days} 天有 {len(window_observations)} 个观测点，跨度 {span_hours:.1f} 小时，不能判断趋势"
        )
    if gap_count:
        warnings.append(f"观测存在 {gap_count} 个超过 36 小时的数据缺口，折线不做插值")
    if declared_children and len(children) < declared_children:
        warnings.append(
            f"楼中楼不完整：已采集 {_number(len(children))}/{_number(declared_children)} 条"
        )
    if latest_observation and expected_replies != platform_replies:
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

    date_counts: Counter[str] = Counter()
    like_counts: Counter[str] = Counter()
    reply_counts: Counter[str] = Counter()
    for row in roots:
        timestamp = int(row["ctime"])
        date = datetime.fromtimestamp(timestamp, timezone.utc).astimezone().strftime("%Y-%m-%d")
        date_counts[date] += 1
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
          ('平台评论数', _number(platform_replies) if latest_observation else '—'),
          ('本地一级评论 + 声明子回复', _number(expected_replies)),
          ('有回复的一级评论', _number(len(roots_with_replies))),
          ('楼中楼完整 / 部分 / 未采集', f'{fully_covered} / {partially_covered} / {untouched}'),
          ('当前抓取状态', completeness),
      ))}
      <h3>视频互动观测</h3>
      <p class="muted">{escape(trend_label)}；最近 {days} 天有 {len(window_observations)} 个观测点，跨度 {span_hours:.1f} 小时。差值仅表示窗口内首个观测与最新观测的差异。</p>
      {_table(('指标', '首次', '最近', '差值'), metric_rows)}
      <div class="chart-grid">{trend_charts}</div>
      <div class="chart-grid">
        {_bar_chart('一级评论发布时间（按日）', sorted(date_counts.items()))}
        {_bar_chart('一级评论点赞分布', [(key, like_counts[key]) for key in ('0', '1–9', '10–99', '100+')])}
        {_bar_chart('一级评论回复数分布', [(key, reply_counts[key]) for key in ('0', '1–9', '10+')])}
      </div>
      {content_html}
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
    sections = []
    totals = defaultdict(int)
    for video in videos:
        section, counts = _video_section(
            database,
            video,
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
.hero{{margin-bottom:22px}} nav{{display:flex;gap:10px;flex-wrap:wrap;margin-top:18px}} nav a{{color:var(--accent);background:var(--accent-soft);padding:5px 10px;border-radius:8px;text-decoration:none}}
.video{{margin-top:22px}} .video-header{{display:flex;align-items:flex-start;justify-content:space-between;gap:18px}} .status{{white-space:nowrap;background:var(--accent-soft);color:var(--accent);padding:6px 10px;border-radius:999px;font-size:13px}}
.cards{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:22px 0}} .card{{border:1px solid var(--line);border-radius:12px;padding:15px}} .card span{{display:block;color:var(--muted)}} .card strong{{font-size:24px}}
.warnings{{padding-left:22px;color:var(--warn)}} .warnings .ok{{color:var(--ok)}} .table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:12px}} table{{width:100%;border-collapse:collapse;min-width:620px}} th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}} th{{background:#f9fafc;font-size:13px}} tr:last-child td{{border-bottom:0}}
.chart-grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px;margin-top:18px}} .chart{{border:1px solid var(--line);border-radius:12px;padding:16px;overflow:hidden}} .chart .table-wrap{{border:0}} .subheading{{margin-top:20px}} .compact{{margin:-8px 0 4px}} svg{{width:100%;height:auto}} svg rect{{fill:var(--accent)}} .trend-line{{fill:none;stroke:var(--accent);stroke-width:3;stroke-linecap:round;stroke-linejoin:round}} svg circle{{fill:var(--surface);stroke:var(--accent);stroke-width:3}} .svg-label,.svg-value{{font-size:13px;fill:var(--text)}} footer{{color:var(--muted);margin-top:28px;text-align:center}}
@media(max-width:760px){{main{{padding:20px 12px 50px}}.cards{{grid-template-columns:repeat(2,1fr)}}.chart-grid{{grid-template-columns:1fr}}.video-header{{display:block}}.status{{display:inline-block;margin-top:10px}}}}
@media print{{body{{background:#fff}}main{{max-width:none;padding:0}}.hero,.video{{box-shadow:none;break-inside:avoid}}nav{{display:none}}}}
</style>
</head>
<body><main>
  <section class="hero">
    <p class="eyebrow">BILI COMMENTS · OFFLINE REPORT</p>
    <h1>采集数据分析报告</h1>
    <p class="muted">生成时间：{escape(generated_at)}。趋势窗口为最近 {days} 天。报告仅包含聚合统计，不包含评论正文或作者标识。</p>
    <div class="cards">
      <div class="card"><span>视频</span><strong>{_number(len(videos))}</strong></div>
      <div class="card"><span>一级评论</span><strong>{_number(totals['roots'])}</strong></div>
      <div class="card"><span>已采集子回复</span><strong>{_number(totals['children'])}</strong></div>
      <div class="card"><span>批次完全成功率</span><strong>{batch_rate}</strong></div>
    </div>
    <p class="muted">抓取运行 {_number(totals['runs'])} 次，其中异常或不完整 {_number(totals['failed_runs'])} 次；批次 {_number(len(batches))} 个。</p>
    <nav>{navigation}</nav>
  </section>
  {''.join(sections)}
  <footer>由 bili-comments 0.5.0 生成 · 单文件离线报告</footer>
</main></body></html>
"""
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")
    return len(videos)
