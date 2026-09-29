"""pick evolve — 按打分与权重动态更新词库。

**默认只预览**（dry-run），只有显式 `--apply` 才动词库——词库是采集的输入，
被改坏会让后续所有周期都跑偏，不该有"手滑就改掉"的可能。

一次 evolve 做三件事：
  1. **换词**：按上一轮 `pick rank` 的打分淘汰长期垫底的词（带迟滞，防抖）；
  2. **扩词**：从快照标题里挖出新候选（词频 + 停用词/排除词过滤），按「出现广度 ×
     平均需求」当代理分排序，每轮限量引入；
  3. **选本轮采集子集**：从词库里按分数挑出预算装得下的那批，写成
     `keywords.next.txt`（并把长期没被采到的尾部词轮换进来，否则它们永远拿不到
     数据、永远无法被评分）。

为什么要选子集：单会话累计约 73 次 mtop 调用就触发 RGV587，而 60 个词 ×
(1 页 + 1 次供给量) = 120 次调用必然触墙。所以词库是**有限预算下的组合**，
不是越大越好——这正是「根据权重更新」的实际含义。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from goofish_cli.commands.pick import _evolve, _store, vocab
from goofish_cli.core import Strategy, command

COLUMNS = ["action", "term", "score", "reason"]

ACTION_LABELS = {"add": "新增", "retire": "淘汰", "bootstrap": "初始化"}


@command(
    namespace="pick",
    name="evolve",
    description="按打分与权重动态更新词库（默认预览，--apply 才写入）",
    strategy=Strategy.PUBLIC,
    columns=COLUMNS,
)
def evolve(
    out: str = "",
    weights: str = "",
    apply: bool = False,
    top_vocab: int = 3000,
) -> dict[str, Any]:
    """`--apply` 才写入 keywords.txt / keywords.next.txt / changelog。"""
    root = Path(out).expanduser() if out else None
    _store.ensure_dirs(root)
    cfg, cfg_source = _store.load_weights(root, weights or None)

    table_path = _store.keywords_path(root)
    existing = _store.load_keywords(root) if table_path.is_file() else []

    report = _store.latest_report(root)
    scores = _store.scores_from_report(report)
    state = _store.load_evolve_state(root)

    obs = _store.load_observations(root)
    records = [_store.latest(series) for series in obs["item"].values()]
    evolve_cfg = cfg.get("evolve") or {}
    # 这里**不能**按出现条数小幅截断：term_stats 是按条数降序的，截到 120 就只剩
    # 样板词，具体好词（如「考试」74 条）全被丢掉、后续覆盖率过滤后一个不剩。
    # 过滤交给 promote_candidates（它掌握覆盖率等真正的判据）。
    candidates = vocab.term_stats(
        records,
        min_titles=int(evolve_cfg.get("min_term_titles") or 8),
        top=max(20, top_vocab),
    )

    promoted = _evolve.promote_candidates(candidates, existing, cfg)
    result = _evolve.select_table(existing, promoted, scores, state, cfg)

    bootstrapped = not existing
    rows: list[dict[str, Any]] = []
    for row in result["add"]:
        rows.append({
            "action": ACTION_LABELS["bootstrap"] if bootstrapped else ACTION_LABELS["add"],
            "term": row["term"],
            "score": row.get("proxy"),
            "reason": row["reason"],
        })
    for row in result["retire"]:
        rows.append({
            "action": ACTION_LABELS["retire"],
            "term": row["term"],
            "score": scores.get(row["term"]),
            "reason": row["reason"],
        })

    final = result["keep"]
    next_path = _store.next_keywords_path(root)
    table_written = False

    if apply:
        # 词库按分数降序写盘：collect 顺序处理，若中途触墙，先保高价值词已采到
        _store.write_keywords(final, root)
        _store.write_keywords(result["cycle"], root, next_path)
        _store.save_evolve_state(result["state"], root)
        _store.append_changelog(
            [{"ts": _store.now_iso(), "cycle": result["cycle_id"], **row} for row in rows],
            root,
        )
        table_written = True

    return {
        "items": rows,
        "applied": table_written,
        "cycle_id": result["cycle_id"],
        "table": str(table_path),
        "next": str(next_path),
        "weights_source": cfg_source,
        "counts": {
            "existing": len(existing),
            "keep": len(result["keep"]),
            "add": len(result["add"]),
            "retire": len(result["retire"]),
            "table": len(final),
            "cycle": len(result["cycle"]),
        },
        "cycle_words": result["cycle"],
        "hint": (
            "已写入。下一轮采集：goofish pick collect keywords.next.txt --pages 1 --run-budget 60"
            if table_written else
            "预览模式（未改动词库）。确认无误后加 --apply 写入。"
        ),
    }


__test__ = {
    "ACTION_LABELS": ACTION_LABELS,
}
