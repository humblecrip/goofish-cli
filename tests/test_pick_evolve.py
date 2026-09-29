"""验词库动态更新：候选过滤、迟滞淘汰、词库上限、本轮子集与轮换、落盘。

核心不变量：**词库是有限预算下的组合**——上限、迟滞、轮换三条都是为了在
风控额度内长期稳定运行，任何一条失效都会让词库无限膨胀或来回抖动。
"""
from __future__ import annotations

import json
from pathlib import Path

from goofish_cli.commands.pick import _evolve
from goofish_cli.commands.pick import evolve as evolve_cmd

CFG = {
    "exclusion": {
        "renewal_strong": ["直充", "日卡"],
        "renewal_weak": ["会员"],
        "human": ["代练", "一对一"],
    },
    "stopwords": ["发货", "秒发", "网盘"],
    "stopwords_exact": ["支持", "软件", "自动"],
    "evolve": {
        "max_keywords": 10,
        "cycle_words": 4,
        "max_promote_per_cycle": 6,
        "new_words_per_cycle": 2,
        "min_term_titles": 5,
        "min_term_coverage": 0.05,
        # 合成语料只有 20 条标题，覆盖率天然偏高（真实语料 1000+ 条）；
        # 测试里放宽上限，专测后续逻辑。
        "max_term_coverage": 0.6,
        "retire_bottom": 2,
        "retire_streak": 2,
        "min_keywords": 2,
        "rotate_slots": 2,
    },
}


# ── 候选过滤 ────────────────────────────────────────────────────────────

def test_rejects_fulfillment_boilerplate_by_substring():
    """履约样板词走**子串**匹配：列表里是「发货」，也要拦住「自动发货」。"""
    for term in ("发货", "自动发货", "秒发货", "网盘链接"):
        ok, reason = _evolve.is_valid_term(term, CFG)
        assert not ok, term
        assert "履约样板词" in reason


def test_generic_words_only_match_exactly():
    """泛化词只精确匹配：「软件」被拦，但「软件安装包」必须放过。"""
    assert not _evolve.is_valid_term("软件", CFG)[0]
    assert _evolve.is_valid_term("软件安装包", CFG)[0], "泛化词不该否决合法品名"


def test_rejects_generic_exact_word():
    ok, reason = _evolve.is_valid_term("支持", CFG)
    assert not ok
    assert "泛化词" in reason


def test_rejects_exclusion_terms():
    assert not _evolve.is_valid_term("代练服务", CFG)[0]
    assert not _evolve.is_valid_term("会员卡", CFG)[0]
    assert not _evolve.is_valid_term("直充秒到", CFG)[0]


def test_rejects_short_and_numeric():
    assert not _evolve.is_valid_term("a", CFG)[0]
    assert not _evolve.is_valid_term("2026", CFG)[0]
    assert _evolve.is_valid_term("考研", CFG)[0]


def test_promote_filters_and_sorts_by_proxy():
    candidates = [
        {"term": "发货", "titles": 99, "coverage": 0.9, "avg_want": 9999},   # 停用词
        {"term": "代练", "titles": 99, "coverage": 0.9, "avg_want": 9999},   # 排除词
        {"term": "考研", "titles": 40, "coverage": 0.3, "avg_want": 100},    # proxy 4000
        {"term": "教资", "titles": 30, "coverage": 0.2, "avg_want": 200},    # proxy 6000
        {"term": "冷门词", "titles": 3, "coverage": 0.01, "avg_want": 999},  # 低于阈值
    ]
    out = _evolve.promote_candidates(candidates, [], CFG)
    assert [r["term"] for r in out] == ["教资", "考研"], "应按 proxy 降序且只留 2 个"


def test_promote_skips_terms_already_covered():
    candidates = [
        {"term": "考研", "titles": 40, "coverage": 0.3, "avg_want": 100},
        {"term": "考研资料", "titles": 40, "coverage": 0.3, "avg_want": 100},
    ]
    out = _evolve.promote_candidates(candidates, ["考研资料"], CFG)
    assert out == [], "与现有词重叠（含子串）的片段不应重复引入"


# ── 迟滞淘汰与上限 ──────────────────────────────────────────────────────

def _scores(pairs: dict[str, float]) -> dict[str, float]:
    return dict(pairs)


def test_low_scorer_needs_consecutive_streak_before_retiring():
    """迟滞：一次垫底不淘汰，避免"加了又删"来回抖动。"""
    existing = ["a", "b", "c", "d"]
    scores = _scores({"a": 90.0, "b": 80.0, "c": 20.0, "d": 10.0})

    first = _evolve.select_table(existing, [], scores, {}, CFG)
    assert first["retire"] == [], "第 1 轮垫底不应淘汰"

    second = _evolve.select_table(first["keep"], [], scores, first["state"], CFG)
    assert {r["term"] for r in second["retire"]} == {"c", "d"}, "连续 2 轮垫底才淘汰"


def test_high_scorer_resets_streak():
    existing = ["a", "b", "c"]
    low = _scores({"a": 90.0, "b": 10.0, "c": 20.0})       # 末尾两名 = b, c
    first = _evolve.select_table(existing, [], low, {}, CFG)
    assert first["state"]["keywords"]["b"]["low_streak"] == 1

    high = _scores({"a": 10.0, "b": 90.0, "c": 20.0})      # b 升到第一，脱离末尾
    second = _evolve.select_table(first["keep"], [], high, first["state"], CFG)
    assert second["state"]["keywords"]["b"]["low_streak"] == 0, "脱离末尾应清零"


def test_small_table_is_never_emptied():
    """下限保护：词库规模不超过 min_keywords 时不做末尾淘汰。

    否则「2 个词 + retire_bottom 2」会让每个词都落在末尾，两轮后整库清空。
    """
    existing = ["a", "b"]
    scores = _scores({"a": 90.0, "b": 10.0})
    state: dict = {}
    for _ in range(5):                                     # 连跑 5 轮
        out = _evolve.select_table(existing, [], scores, state, CFG)
        state = out["state"]
        assert len(out["keep"]) == 2, "小词库不应被淘汰清空"
        assert out["retire"] == []


def test_max_keywords_caps_table():
    existing = [f"k{i}" for i in range(15)]
    scores = {f"k{i}": float(100 - i) for i in range(15)}
    out = _evolve.select_table(existing, [], scores, {}, CFG)
    assert len(out["keep"]) <= CFG["evolve"]["max_keywords"]
    assert any("上限" in r["reason"] for r in out["retire"])
    assert "k14" in {r["term"] for r in out["retire"]}, "最低分的先出局"


def test_table_keeps_new_words_with_unknown_score():
    """新词还没有分数，但必须进词库——否则永远拿不到数据。"""
    promoted = [{"term": "新词", "reason": "新候选", "proxy": 1.0}]
    out = _evolve.select_table(["老词"], promoted, {"老词": 50.0}, {}, CFG)
    assert "新词" in out["keep"], "keep 就是最终词库成员（含新增）"
    assert "老词" in out["keep"]


# ── 本轮采集子集与轮换 ──────────────────────────────────────────────────

def test_cycle_takes_top_by_score():
    existing = ["a", "b", "c", "d", "e", "f"]
    scores = {k: float(v) for k, v in zip(existing, [10, 90, 50, 20, 80, 30], strict=True)}
    out = _evolve.select_table(existing, [], scores, {}, CFG)
    assert len(out["cycle"]) == CFG["evolve"]["cycle_words"]
    assert set(out["cycle"]) == {"b", "e", "c", "f"}, "没有饥饿词时按分数取前 4"


def test_starved_keywords_rotate_into_cycle():
    """长期没被采到的尾部词必须轮换进来，否则永远无法被评分。"""
    existing = ["a", "b", "c", "d", "e", "f"]
    scores = {k: float(v) for k, v in zip(existing, [100, 90, 80, 70, 1, 0], strict=True)}
    # c/d 刚采过（本轮），e/f 很久没采 → 应轮换 e/f 进来
    state = {"cycle": 5, "keywords": {
        "c": {"last_collected": 5}, "d": {"last_collected": 5},
        "e": {"last_collected": 1}, "f": {"last_collected": 2},
    }}
    out = _evolve.select_table(existing, [], scores, state, CFG)
    assert len(out["cycle"]) == CFG["evolve"]["cycle_words"]
    assert {"e", "f"} <= set(out["cycle"]), "饥饿词应被轮换进来"


def test_cycle_marks_last_collected():
    existing = ["a", "b"]
    out = _evolve.select_table(existing, [], {"a": 10.0, "b": 5.0}, {}, CFG)
    assert out["state"]["cycle"] == 1
    for keyword in out["cycle"]:
        assert out["state"]["keywords"][keyword]["last_collected"] == 1


# ── 命令层：默认不写 ────────────────────────────────────────────────────

def _seed_corpus(root: Path) -> None:

    (root / "snapshots").mkdir(parents=True, exist_ok=True)
    recs = [
        {"type": "item", "ts": "2026-01-01T10:00:00+08:00", "item_id": str(i),
         "keyword": "考研 资料", "keywords": ["考研 资料"],
         "title": "考研资料 全套 真题 讲义 笔记" if i % 2 else "教资资料 全套 真题 讲义",
         "want": 100 - i, "price_num": 1.0, "seller_rate_count": 10,
         "seller_uid": f"u{i}", "seller_nick": f"n{i}"}
        for i in range(20)
    ]
    path = root / "snapshots" / "20260101-100000.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def test_dry_run_does_not_touch_table(tmp_path):
    _seed_corpus(tmp_path)
    out = evolve_cmd.evolve(out=str(tmp_path), weights=json.dumps(CFG), apply=False)
    assert out["applied"] is False
    assert not (tmp_path / "keywords.txt").exists(), "预览模式不应创建词库"
    assert "预览模式" in out["hint"]


def test_apply_writes_table_next_state_and_changelog(tmp_path):
    _seed_corpus(tmp_path)
    # 显式传测试配置：包内默认 max_term_coverage=0.12 是给千条级真实语料调的，
    # 20 条合成语料的覆盖率天然 1.0，会被全滤掉。
    out = evolve_cmd.evolve(out=str(tmp_path), weights=json.dumps(CFG), apply=True)

    assert out["applied"] is True
    table = (tmp_path / "keywords.txt").read_text(encoding="utf-8").split()
    assert table, "应写出词库"
    assert len(table) <= 60

    next_words = (tmp_path / "keywords.next.txt").read_text(encoding="utf-8").split()
    assert next_words == out["cycle_words"]

    state = json.loads((tmp_path / "evolve_state.json").read_text(encoding="utf-8"))
    assert state["cycle"] == 1

    changelog = (tmp_path / "keywords.changelog.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert changelog, "每次增删都要留痕，否则无法回溯词库为什么变成这样"
    assert all(json.loads(line)["ts"] for line in changelog)


def test_apply_is_idempotent_on_second_run(tmp_path):
    """再跑一次不应把词库清空或重复追加（词库是输入，抖一次代价很大）。"""
    _seed_corpus(tmp_path)
    evolve_cmd.evolve(out=str(tmp_path), weights=json.dumps(CFG), apply=True)
    first = (tmp_path / "keywords.txt").read_text(encoding="utf-8").split()

    evolve_cmd.evolve(out=str(tmp_path), weights=json.dumps(CFG), apply=True)
    second = (tmp_path / "keywords.txt").read_text(encoding="utf-8").split()

    assert second, "第二次不应清空"
    assert set(second) >= set(first), "已有词不应无故消失"
    assert len(second) == len(set(second)), "不应出现重复词"
