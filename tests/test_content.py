import pytest

from bili_comments import content
from bili_comments.content import analyze_messages
from bili_comments.errors import ConfigurationError


class FakeJieba:
    __version__ = "test"

    @staticmethod
    def lcut(sentence: str, cut_all: bool = False) -> list[str]:
        assert cut_all is False
        return sentence.split()


def test_normalization_frequency_tfidf_and_cooccurrence() -> None:
    messages = [
        "数据 数据 分析 http://example.com @用户 BV1xx411c7mD 123 这个",
        "数据 分析 报告",
        "数据 分析 报告",
        "数据 可视化",
    ]
    result = analyze_messages(messages, jieba=FakeJieba())
    assert result.analyzer_version == "test"
    assert result.document_count == 4
    assert [(item.token, item.count, item.document_frequency) for item in result.frequencies] == [
        ("数据", 5, 4),
        ("分析", 3, 3),
    ]
    assert [item.token for item in result.tfidf] == ["数据", "分析"]
    assert [(item.left, item.right, item.count) for item in result.cooccurrences] == [
        ("分析", "数据", 3)
    ]
    assert all(
        value not in {item.token for item in result.frequencies}
        for value in ("用户", "123", "这个", "bv1xx411c7md")
    )


def test_missing_optional_dependency_has_install_hint(monkeypatch) -> None:
    def missing(_name: str):
        raise ImportError

    monkeypatch.setattr(content.importlib, "import_module", missing)
    with pytest.raises(ConfigurationError, match=r"\[analysis\]"):
        analyze_messages(["数据 分析"])


def test_complete_single_token_comments_are_not_exposed() -> None:
    result = analyze_messages(
        ["隐私 主题", "隐私 讨论", "隐私 内容", "隐私"], jieba=FakeJieba()
    )
    assert "隐私" not in {item.token for item in result.frequencies}
    assert "隐私" not in {item.token for item in result.tfidf}


@pytest.mark.analysis
def test_real_jieba_analysis() -> None:
    pytest.importorskip("jieba")
    result = analyze_messages(["数据分析报告"] * 3)
    assert result.analyzer_version != "unknown"
    assert result.document_count == 3
