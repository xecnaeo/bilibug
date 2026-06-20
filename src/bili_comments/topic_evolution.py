from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .content import (
    JiebaModule,
    Keyword,
    analyze_messages,
    load_jieba,
    single_token_terms,
)
from .lifecycle import HOUR

MIN_STAGE_COMMENTS = 20
STAGE_KEYWORD_LIMIT = 10
HEATMAP_TERM_LIMIT = 20


@dataclass(frozen=True)
class StageDefinition:
    key: str
    label: str
    start_hours: float
    end_hours: float | None


STAGES = (
    StageDefinition("h0_6", "0–6小时", 0, 6),
    StageDefinition("h6_24", "6–24小时", 6, 24),
    StageDefinition("d1_3", "1–3天", 24, 72),
    StageDefinition("d3_7", "3–7天", 72, 168),
    StageDefinition("d7_plus", "7天后", 168, None),
)


@dataclass(frozen=True)
class StageTopic:
    definition: StageDefinition
    status: str
    comment_count: int
    comment_share: float
    eligible: bool
    keywords: tuple[Keyword, ...]
    new_terms: tuple[str, ...] = ()


@dataclass(frozen=True)
class HeatmapTerm:
    token: str
    weights: tuple[float, ...]


@dataclass(frozen=True)
class TopicEvolution:
    analyzer_version: str
    total_comment_count: int
    invalid_comment_count: int
    stages: tuple[StageTopic, ...]
    heatmap_terms: tuple[HeatmapTerm, ...]
    persistent_terms: tuple[str, ...]
    comparable: bool


def _stage_for(hours: float) -> int:
    for index, stage in enumerate(STAGES):
        if hours >= stage.start_hours and (
            stage.end_hours is None or hours < stage.end_hours
        ):
            return index
    return len(STAGES) - 1


def _stage_status(stage: StageDefinition, age_hours: float) -> str:
    if stage.end_hours is None:
        return "ongoing"
    if age_hours < stage.start_hours:
        return "not_started"
    if age_hours < stage.end_hours:
        return "ongoing"
    return "complete"


def analyze_topic_evolution(
    comments: Sequence[Mapping[str, object]],
    *,
    published_at: int,
    cutoff_at: int,
    jieba: JiebaModule | None = None,
) -> TopicEvolution | None:
    if published_at <= 0 or cutoff_at < published_at:
        return None
    analyzer = jieba or load_jieba()
    stage_messages: list[list[str]] = [[] for _ in STAGES]
    all_messages: list[str] = []
    privacy_messages: list[str] = []
    invalid = 0
    for row in comments:
        if int(row["level"]) != 0:
            continue
        message = str(row["message"])
        privacy_messages.append(message)
        try:
            ctime = int(row["ctime"])
        except (TypeError, ValueError):
            invalid += 1
            continue
        if ctime < published_at or ctime > cutoff_at or ctime <= 0:
            invalid += 1
            continue
        hours = (ctime - published_at) / HOUR
        stage_messages[_stage_for(hours)].append(message)
        all_messages.append(message)

    blocked = single_token_terms(privacy_messages, jieba=analyzer)
    total = len(all_messages)
    age_hours = (cutoff_at - published_at) / HOUR
    raw_stages: list[StageTopic] = []
    for definition, messages in zip(STAGES, stage_messages):
        status = _stage_status(definition, age_hours)
        mature = status == "complete" or (
            definition.end_hours is None and status == "ongoing"
        )
        eligible = mature and len(messages) >= MIN_STAGE_COMMENTS
        keywords: tuple[Keyword, ...] = ()
        if eligible:
            analysis = analyze_messages(
                messages,
                jieba=analyzer,
                minimum_document_frequency=3,
                frequency_limit=STAGE_KEYWORD_LIMIT,
                excluded_tokens=blocked,
            )
            keywords = analysis.tfidf
        raw_stages.append(
            StageTopic(
                definition=definition,
                status=status,
                comment_count=len(messages),
                comment_share=len(messages) / total if total else 0.0,
                eligible=eligible,
                keywords=keywords,
            )
        )

    stages = list(raw_stages)
    for index in range(1, len(stages)):
        previous, current = stages[index - 1], stages[index]
        if not previous.eligible or not current.eligible:
            continue
        previous_tokens = {item.token for item in previous.keywords}
        new_terms = tuple(
            item.token for item in current.keywords if item.token not in previous_tokens
        )
        stages[index] = StageTopic(
            definition=current.definition,
            status=current.status,
            comment_count=current.comment_count,
            comment_share=current.comment_share,
            eligible=current.eligible,
            keywords=current.keywords,
            new_terms=new_terms,
        )

    token_scores: dict[str, list[float]] = {}
    token_stage_counts: dict[str, int] = {}
    for index, stage in enumerate(stages):
        for keyword in stage.keywords:
            token_scores.setdefault(keyword.token, [0.0] * len(STAGES))[index] = keyword.score
            token_stage_counts[keyword.token] = token_stage_counts.get(keyword.token, 0) + 1
    selected_tokens = sorted(
        token_scores,
        key=lambda token: (-max(token_scores[token]), token),
    )[:HEATMAP_TERM_LIMIT]
    heatmap = []
    for token in selected_tokens:
        weights = []
        for index, score in enumerate(token_scores[token]):
            maximum = max((item.score for item in stages[index].keywords), default=0.0)
            weights.append(score / maximum if maximum else 0.0)
        heatmap.append(HeatmapTerm(token=token, weights=tuple(weights)))
    persistent = tuple(
        sorted(
            (token for token, count in token_stage_counts.items() if count >= 3),
            key=lambda token: (-token_stage_counts[token], -max(token_scores[token]), token),
        )
    )
    eligible_count = sum(stage.eligible for stage in stages)
    return TopicEvolution(
        analyzer_version=str(getattr(analyzer, "__version__", "unknown")),
        total_comment_count=total,
        invalid_comment_count=invalid,
        stages=tuple(stages),
        heatmap_terms=tuple(heatmap),
        persistent_terms=persistent,
        comparable=eligible_count >= 2,
    )
