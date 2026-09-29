"""验选品排除规则（强/弱续费词分层 + 组合价格条件 + 人工介入型）。"""
from __future__ import annotations

import pytest

from goofish_cli.commands.pick import _scoring

EXCLUSION = {
    "renewal_strong": ["直充", "代充", "日卡", "年卡"],
    "renewal_weak": ["会员", "vip"],
    "human": ["代练", "一对一", "定制"],
    "low_price_ceiling": 5.0,
}


def _rec(title: str, price: float | None = None) -> dict:
    return {"title": title, "price_num": price}


@pytest.mark.parametrize("title", [
    "芒果会员日卡直充秒到",
    "腾讯视频月卡年卡代充",
    "某某卡密直充",
])
def test_strong_renewal_always_excluded(title):
    """强续费词本身就是服务型商品的强标识——不看价格也排除。"""
    excluded, reason = _scoring.classify(_rec(title, 99.0), EXCLUSION)
    assert excluded
    assert "续费型" in reason


@pytest.mark.parametrize("title,price", [
    ("B站会员追剧合集", 3.0),
    ("vip专属素材包", 1.5),
])
def test_weak_renewal_excluded_only_when_cheap(title, price):
    """弱词（会员/vip）可能出现在资料标题里 —— 只有价格也低时才判为续费型。"""
    excluded, reason = _scoring.classify(_rec(title, price), EXCLUSION)
    assert excluded
    assert "组合" in reason


def test_weak_renewal_kept_when_expensive():
    """同样是「会员」二字，高价时不该被当成续费型——否则会误杀。"""
    excluded, reason = _scoring.classify(_rec("正版会员制课程 完整版", 199.0), EXCLUSION)
    assert not excluded
    assert reason == ""


@pytest.mark.parametrize("title", [
    "火影忍者代练日常",
    "考研一对一辅导录音",
    "论文定制写作模板",
])
def test_human_intervention_excluded(title):
    excluded, reason = _scoring.classify(_rec(title, 50.0), EXCLUSION)
    assert excluded
    assert "人工介入" in reason


def test_human_reason_wins_over_renewal():
    """同时命中两类时，人工介入型的理由优先（更具体、更能解释为什么排除）。"""
    excluded, reason = _scoring.classify(_rec("会员代练服务日卡", 1.0), EXCLUSION)
    assert excluded
    assert "人工介入" in reason


@pytest.mark.parametrize("title,price", [
    ("2026 考研 全套资料 电子版", 9.9),
    ("别墅庭院设计素材 源文件", 1.8),
    ("剪辑软件安装包 win/mac", 6.0),
])
def test_target_deliverables_kept(title, price):
    """「交付即完」的数字交付物不该被任何规则误杀。"""
    excluded, reason = _scoring.classify(_rec(title, price), EXCLUSION)
    assert not excluded, reason


def test_missing_price_does_not_exclude_weak_word():
    """价格缺失时，弱词不足以判为续费型（宁可保留、交给人工复核）。"""
    excluded, _ = _scoring.classify(_rec("会员素材", None), EXCLUSION)
    assert not excluded


def test_price_boundary_is_inclusive():
    excluded, _ = _scoring.classify(_rec("会员素材", 5.0), EXCLUSION)
    assert excluded                      # 边界值算命中（<=）
