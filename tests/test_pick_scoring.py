"""验打分层：分位归一、加权求和、缺维度降级重归一。"""
from __future__ import annotations

import pytest

from goofish_cli.commands.pick import _scoring

# ── 分位归一 ────────────────────────────────────────────────────────────

def test_quantile_spreads_endpoints():
    assert _scoring.quantile_scores([10.0, 20.0, 30.0]) == [0.0, 50.0, 100.0]


def test_quantile_keeps_none():
    out = _scoring.quantile_scores([5.0, None, 15.0])
    assert out[1] is None
    assert out[0] == 0.0
    assert out[2] == 100.0


def test_quantile_ties_share_average_rank():
    """并列值取平均名次 —— 否则同分商品的排序会随输入顺序漂移。"""
    out = _scoring.quantile_scores([7.0, 7.0, 9.0])
    assert out[0] == out[1]
    assert out[0] == pytest.approx(25.0)
    assert out[2] == 100.0


def test_quantile_single_value_is_neutral():
    """只有一个有效样本时分位无意义，记 50 中性值，避免制造假的高低差。"""
    assert _scoring.quantile_scores([42.0]) == [50.0]


def test_quantile_all_none():
    assert _scoring.quantile_scores([None, None]) == [None, None]


# ── 加权与降级 ──────────────────────────────────────────────────────────

# 用真实维度名：add_scores 只认 _scoring.DIMENSIONS 里的维度，
# 配置里的陌生维度会被忽略（防止用户配置写错维度名后静默生效）。
WEIGHTS = {
    "dimensions": {
        "imbalance": {"weight": 0.5},
        "want": {"weight": 0.5},
    }
}


def test_add_scores_weighted_sum():
    rows = [{"imbalance_raw": 1.0, "want_raw": 1.0}, {"imbalance_raw": 2.0, "want_raw": 2.0}]
    _scoring.add_scores(rows, WEIGHTS)
    assert rows[0]["score"] == 0.0
    assert rows[1]["score"] == 100.0
    assert rows[0]["dims_used"] == "imbalance,want"
    assert rows[0]["dims_degraded"] == ""


def test_missing_dimension_is_renormalized_not_zeroed():
    """缺数据的维度应**退出加权**并重新归一，而不是当 0 分参与。

    否则单快照时（成交速率算不出来）所有关键词的分数都会被那个 0 拖成假的低值。
    """
    rows = [
        {"imbalance_raw": 1.0, "want_raw": None},
        {"imbalance_raw": 2.0, "want_raw": None},
    ]
    _scoring.add_scores(rows, WEIGHTS)
    assert rows[0]["score"] == 0.0
    assert rows[1]["score"] == 100.0          # 若把缺失维当 0 分，这里会远低于 100
    assert rows[0]["dims_used"] == "imbalance"
    assert rows[0]["dims_degraded"] == "want"


def test_zero_weight_dimension_is_unknown_not_used():
    weights = {"dimensions": {"imbalance": {"weight": 1.0}, "want": {"weight": 0.0}}}
    rows = [{"imbalance_raw": 1.0, "want_raw": 9.0}, {"imbalance_raw": 3.0, "want_raw": 9.0}]
    _scoring.add_scores(rows, weights)
    assert rows[0]["dims_used"] == "imbalance"
    assert rows[1]["score"] == 100.0


def test_no_usable_dimension_gives_none_score():
    rows = [{"imbalance_raw": None, "want_raw": None}]
    _scoring.add_scores(rows, WEIGHTS)
    assert rows[0]["score"] is None


def test_dimensions_subset_of_config():
    """配置里没写的维度不参与（便于临时关掉某一维做对照实验）。"""
    weights = {"dimensions": {"imbalance": {"weight": 1.0}}}
    rows = [{"imbalance_raw": 1.0, "want_raw": 1.0}, {"imbalance_raw": 5.0, "want_raw": 1.0}]
    _scoring.add_scores(rows, weights)
    assert "want_norm" not in rows[0]
    assert rows[1]["score"] == 100.0


def test_median_helper():
    assert _scoring.median([]) is None
    assert _scoring.median([3.0]) == 3.0
    assert _scoring.median([1.0, 3.0]) == 2.0
    assert _scoring.median([5.0, 1.0, 3.0]) == 3.0
