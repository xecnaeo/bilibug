from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Mapping, Sequence

from .content import ContentAnalysis, analyze_messages, load_jieba
from .database import Database
from .errors import ConfigurationError
from .lifecycle import (
    LifecycleAnalysis,
    ThreadAnalysis,
    analyze_lifecycle,
    analyze_threads,
    compare_lifecycles,
)
from .report_render import (
    bar_chart as _bar_chart,
    comparison_section as _comparison_chart,
    content_section as _content_section,
    lifecycle_section as _lifecycle_section,
    number as _number,
    observation_line_chart as _line_chart,
    percent as _percent,
    table as _table,
    thread_section as _thread_section,
    timestamp as _timestamp,
    topic_evolution_section as _topic_evolution_section,
)
from .topic_evolution import TopicEvolution, analyze_topic_evolution

METRICS = (
    ("view_count", "播放"),
    ("like_count", "点赞"),
    ("favorite_count", "收藏"),
    ("coin_count", "投币"),
    ("share_count", "分享"),
    ("reply_count", "评论"),
)


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


def _lifecycle_confidence(
    *, complete_root_run: bool, platform_replies: int | None, expected_replies: int
) -> str:
    if not complete_root_run:
        return "低"
    if platform_replies is not None and platform_replies == expected_replies:
        return "高"
    return "中"


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


def _content_rows(database: Database, bvid: str) -> list[Mapping[str, object]]:
    return list(
        database.connection.execute(
            """
            SELECT message, ctime, level
            FROM comments WHERE bvid = ? AND level = 0 ORDER BY rpid
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


def _video_section(
    database: Database,
    video: Mapping[str, object],
    comments: Sequence[Mapping[str, object]],
    observations: Sequence[Mapping[str, object]],
    lifecycle: LifecycleAnalysis | None,
    threads: ThreadAnalysis | None,
    content: ContentAnalysis | None,
    topics: TopicEvolution | None,
    *,
    complete_root_run: bool,
    days: int,
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
    content_html = _content_section(content) if content is not None else ""
    topic_html = _topic_evolution_section(topics) if content is not None else ""
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
      {topic_html}
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
    analyzer = load_jieba() if content_analysis else None
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
        content = None
        topics = None
        if analyzer is not None:
            text_rows = _content_rows(database, bvid)
            content = analyze_messages(
                [str(row["message"]) for row in text_rows], jieba=analyzer
            )
            topics = analyze_topic_evolution(
                text_rows,
                published_at=published_at,
                cutoff_at=cutoff,
                jieba=analyzer,
            )
        complete_root_run = complete_all_run or _has_complete_run(database, bvid, "root")
        report_items.append(
            (
                video,
                comments,
                observations,
                lifecycle,
                threads,
                content,
                topics,
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
        content,
        topics,
        complete_root_run,
    ) in report_items:
        section, counts = _video_section(
            database,
            video,
            comments,
            observations,
            lifecycle,
            threads,
            content,
            topics,
            complete_root_run=complete_root_run,
            days=days,
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
.warnings{{padding-left:22px;color:var(--warn)}} .warnings .ok{{color:var(--ok)}} .table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:12px}} table{{width:100%;border-collapse:collapse;min-width:620px}} th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}} th{{background:#f9fafc;font-size:13px}} tr:last-child td{{border-bottom:0}} .heatmap .heat{{background:rgba(61,101,216,var(--weight));text-align:center}}
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
  <footer>由 bili-comments 0.7.0 生成 · 单文件离线报告</footer>
</main></body></html>
"""
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html, encoding="utf-8")
    return len(videos)
