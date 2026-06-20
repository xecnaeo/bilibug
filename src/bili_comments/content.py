from __future__ import annotations

import importlib
import logging
import math
import re
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from itertools import combinations
from typing import Protocol

from .database import Database
from .errors import ConfigurationError

MIN_DOCUMENT_FREQUENCY = 3
FREQUENCY_LIMIT = 20
COOCCURRENCE_VOCABULARY = 30
COOCCURRENCE_LIMIT = 20

STOPWORDS = frozenset(
    {
        "一个",
        "一些",
        "一样",
        "不是",
        "不能",
        "不过",
        "什么",
        "他们",
        "你们",
        "但是",
        "因为",
        "所以",
        "这个",
        "那个",
        "这些",
        "那些",
        "然后",
        "如果",
        "已经",
        "还是",
        "没有",
        "可以",
        "可能",
        "就是",
        "觉得",
        "真的",
        "自己",
        "我们",
        "怎么",
        "这种",
        "这里",
        "这样",
        "现在",
        "时候",
        "非常",
        "and",
        "are",
        "for",
        "the",
        "this",
        "that",
        "with",
    }
)

URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
MENTION_PATTERN = re.compile(r"@[A-Za-z0-9_\-\u3400-\u9fff]+")
VIDEO_ID_PATTERN = re.compile(r"\b(?:BV[A-Za-z0-9]{10}|av\d+)\b", re.IGNORECASE)
TOKEN_PATTERN = re.compile(r"(?:[a-z][a-z0-9_+\-]*|[\u3400-\u9fff]+)", re.IGNORECASE)


class JiebaModule(Protocol):
    __version__: str

    def lcut(self, sentence: str, cut_all: bool = False) -> list[str]: ...


@dataclass(frozen=True)
class Keyword:
    token: str
    count: int
    document_frequency: int
    score: float = 0.0


@dataclass(frozen=True)
class Cooccurrence:
    left: str
    right: str
    count: int


@dataclass(frozen=True)
class ContentAnalysis:
    analyzer_version: str
    document_count: int
    frequencies: tuple[Keyword, ...]
    tfidf: tuple[Keyword, ...]
    cooccurrences: tuple[Cooccurrence, ...]


def _load_jieba() -> JiebaModule:
    try:
        module = importlib.import_module("jieba")
    except ImportError as exc:
        raise ConfigurationError(
            '内容分析需要可选依赖，请执行 pip install -e ".[analysis]"'
        ) from exc
    set_log_level = getattr(module, "setLogLevel", None)
    if callable(set_log_level):
        set_log_level(logging.WARNING)
    return module


def load_jieba() -> JiebaModule:
    return _load_jieba()


def tokenize_message(message: str, jieba: JiebaModule) -> tuple[str, ...]:
    cleaned = VIDEO_ID_PATTERN.sub(" ", MENTION_PATTERN.sub(" ", URL_PATTERN.sub(" ", message)))
    tokens = []
    for value in jieba.lcut(cleaned, cut_all=False):
        token = value.strip().lower()
        if (
            len(token) >= 2
            and TOKEN_PATTERN.fullmatch(token)
            and token not in STOPWORDS
            and not token.isdigit()
        ):
            tokens.append(token)
    return tuple(tokens)


def single_token_terms(
    messages: Collection[str], *, jieba: JiebaModule
) -> frozenset[str]:
    return frozenset(
        document[0]
        for message in messages
        if len(document := tokenize_message(message, jieba)) == 1
    )


def analyze_messages(
    messages: list[str],
    *,
    jieba: JiebaModule | None = None,
    minimum_document_frequency: int = MIN_DOCUMENT_FREQUENCY,
    frequency_limit: int = FREQUENCY_LIMIT,
    excluded_tokens: Collection[str] = (),
) -> ContentAnalysis:
    analyzer = jieba or _load_jieba()
    documents = [tokenize_message(message, analyzer) for message in messages]
    counts: Counter[str] = Counter(token for document in documents for token in document)
    document_frequencies: Counter[str] = Counter(
        token for document in documents for token in set(document)
    )
    complete_single_token_comments = {
        document[0] for document in documents if len(document) == 1
    } | set(excluded_tokens)
    eligible = {
        token
        for token, frequency in document_frequencies.items()
        if frequency >= minimum_document_frequency
        and token not in complete_single_token_comments
    }
    frequency_items = sorted(eligible, key=lambda token: (-counts[token], token))[
        :frequency_limit
    ]
    frequencies = tuple(
        Keyword(token, counts[token], document_frequencies[token])
        for token in frequency_items
    )

    total_tokens = sum(counts[token] for token in eligible) or 1
    document_count = len(documents)
    scores = {
        token: (counts[token] / total_tokens)
        * (math.log((document_count + 1) / (document_frequencies[token] + 1)) + 1)
        for token in eligible
    }
    tfidf_items = sorted(eligible, key=lambda token: (-scores[token], token))[
        :frequency_limit
    ]
    tfidf = tuple(
        Keyword(token, counts[token], document_frequencies[token], scores[token])
        for token in tfidf_items
    )

    vocabulary = set(
        sorted(
            eligible,
            key=lambda token: (-document_frequencies[token], -counts[token], token),
        )[:COOCCURRENCE_VOCABULARY]
    )
    pair_counts: Counter[tuple[str, str]] = Counter()
    for document in documents:
        terms = sorted(set(document) & vocabulary)
        pair_counts.update(combinations(terms, 2))
    pairs = sorted(
        (pair, count) for pair, count in pair_counts.items() if count >= 2
    )
    pairs.sort(key=lambda item: (-item[1], item[0][0], item[0][1]))
    cooccurrences = tuple(
        Cooccurrence(left, right, count)
        for (left, right), count in pairs[:COOCCURRENCE_LIMIT]
    )
    return ContentAnalysis(
        analyzer_version=str(getattr(analyzer, "__version__", "unknown")),
        document_count=document_count,
        frequencies=frequencies,
        tfidf=tfidf,
        cooccurrences=cooccurrences,
    )


def analyze_video(database: Database, bvid: str) -> ContentAnalysis:
    messages = [
        str(row["message"])
        for row in database.connection.execute(
            "SELECT message FROM comments WHERE bvid = ? AND level = 0 ORDER BY rpid",
            (bvid,),
        )
    ]
    return analyze_messages(messages)
