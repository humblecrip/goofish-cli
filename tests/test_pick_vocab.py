"""验词频收敛：n-gram 抽取与子串去噪。"""
from __future__ import annotations

from goofish_cli.commands.pick import vocab


def test_ngrams_strips_punctuation_and_digits():
    grams = vocab._ngrams("2026考研资料，全套！", low=2, high=4)
    assert "考研" in grams
    assert "资料" in grams
    assert "考研资料" in grams
    assert "2026" not in grams
    assert not any(g.isdigit() for g in grams)
    assert not any("," in g or "，" in g or "！" in g for g in grams)


def test_ngrams_respects_size_bounds():
    grams = vocab._ngrams("考研资料", low=2, high=2)
    assert all(len(g) == 2 for g in grams)
    assert grams == {"考研", "研资", "资料"}


def test_ngrams_returns_set_for_document_frequency():
    """必须返回集合：同一条标题里重复出现不该被算两次，否则长标题会刷出噪声。"""
    assert vocab._ngrams("资料资料资料", low=2, high=2) == {"资料", "料资"}


def test_prune_drops_redundant_substring():
    cands = {
        "资料合集": {"term": "资料合集", "grams": 4, "titles": 100},
        "料合集": {"term": "料合集", "grams": 3, "titles": 101},   # 子串且频次接近
    }
    kept = vocab._prune(cands)
    assert [r["term"] for r in kept] == ["资料合集"]


def test_prune_keeps_distinct_terms():
    cands = {
        "考研": {"term": "考研", "grams": 2, "titles": 90},
        "考公": {"term": "考公", "grams": 2, "titles": 80},
    }
    kept = vocab._prune(cands)
    assert {r["term"] for r in kept} == {"考研", "考公"}


def test_prune_keeps_substring_that_is_much_more_frequent():
    """子串但频次显著更高时保留 —— 它可能是独立的常用词而不是冗余片段。"""
    cands = {
        "设计素材": {"term": "设计素材", "grams": 4, "titles": 50},
        "素材": {"term": "素材", "grams": 2, "titles": 300},
    }
    kept = {r["term"] for r in vocab._prune(cands)}
    assert "素材" in kept
