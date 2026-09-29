"""pick vocab — 从标题语料里抽候选词频表，用来建受管关键词表。

为什么需要它：闲鱼搜索是**标题匹配**，猜词表基本会废（照字面写「文献 pdf」大概率
搜不到东西）。所以流程是「先宽搜一轮 → 从真实标题里抽高频词 → 人工划掉不属于目标
类目的 → 收敛成正式词表」。

分词用**无依赖的 n-gram**（2-4 字），不是 jieba：
  - 产出是"30-50 个候选给你划掉"，噪声可容忍（你本来就要人工过一遍）；
  - 给本包加中文分词硬依赖，对上游是额外的接收阻力。
分词实现放在 `_ngrams`，将来要换 jieba 只改这一个函数。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from goofish_cli.commands.pick import _store
from goofish_cli.core import Strategy, command

COLUMNS = [
    "term", "grams", "titles", "coverage", "occurrences",
    "avg_want", "max_want", "samples",
]
MIN_GRAM = 2
MAX_GRAM = 4
_SKIP_CHARS = set("0123456789 \t　·,，.。!！?？:：;；、/\\|-—_+*#@()（）[]【】{}<>《》\"'“”‘’~&%$^")


def _ngrams(title: str, low: int = MIN_GRAM, high: int = MAX_GRAM) -> set[str]:
    """抽取标题里所有 2-4 字候选片段（去标点、去纯数字）。

    返回 set —— 统计的是**文档频次**（多少条标题含该片段），不是出现次数，
    否则一条长标题会把某个片段刷成高频噪声。
    """
    cleaned = "".join(ch for ch in title if ch not in _SKIP_CHARS)
    out: set[str] = set()
    for size in range(low, high + 1):
        for i in range(len(cleaned) - size + 1):
            gram = cleaned[i:i + size]
            if gram.isdigit():
                continue
            out.add(gram)
    return out


def _prune(candidates: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """丢弃"被更长且同样高频的片段覆盖"的短片段。

    例：同时存在「资料合集」(120) 与「料合集」(121) → 后者是前者的子串且频次接近，
    保留长的那个，丢掉短的，否则候选表会被子串刷满。
    """
    # 必须先长后短：冗余判定是"当前词是否是某个**已保留**词的子串"，
    # 若先处理短词，长词永远不会被判为冗余（它不可能是短词的子串）。
    rows = sorted(candidates.values(), key=lambda r: (-r["grams"], -r["titles"], r["term"]))
    kept: list[dict[str, Any]] = []
    for row in rows:
        redundant = False
        for other in kept:
            if row["term"] in other["term"] and row["titles"] <= other["titles"] * 1.1:
                redundant = True
                break
        if not redundant:
            kept.append(row)
    return kept


@command(
    namespace="pick",
    name="vocab",
    description="从快照标题抽候选词频表（无依赖 n-gram），用于收敛出关键词表",
    strategy=Strategy.PUBLIC,
    columns=COLUMNS,
)
def vocab(
    out: str = "",
    top: int = 60,
    min_titles: int = 3,
) -> dict[str, Any]:
    """读全部快照的标题，输出去重后的候选词。"""
    root = Path(out).expanduser() if out else None
    obs = _store.load_observations(root)
    records = [_store.latest(series) for series in obs["item"].values()]
    if not records:
        return {"items": [], "note": "没有快照数据，先跑 `goofish pick collect`"}

    total = len(records)
    stats: dict[str, dict[str, Any]] = {}
    for rec in records:
        title = str(rec.get("title") or "")
        if not title:
            continue
        want = rec.get("want") if isinstance(rec.get("want"), (int, float)) else None
        for gram in _ngrams(title):
            row = stats.setdefault(gram, {
                "term": gram, "grams": len(gram), "titles": 0, "occurrences": 0,
                "want_sum": 0.0, "want_n": 0, "max_want": 0,
                "_samples": [],
            })
            row["titles"] += 1
            row["occurrences"] += title.count(gram)
            if want is not None:
                row["want_sum"] += want
                row["want_n"] += 1
                row["max_want"] = max(row["max_want"], int(want))
            if len(row["_samples"]) < 3 and title not in row["_samples"]:
                row["_samples"].append(title)

    candidates = {k: v for k, v in stats.items() if v["titles"] >= max(1, min_titles)}
    kept = _prune(candidates)
    kept.sort(key=lambda r: (-r["titles"], -r["grams"]))

    rows: list[dict[str, Any]] = []
    for row in kept[:max(1, top)]:
        rows.append({
            "term": row["term"],
            "grams": row["grams"],
            "titles": row["titles"],
            "coverage": round(row["titles"] / total, 3),
            "occurrences": row["occurrences"],
            "avg_want": round(row["want_sum"] / row["want_n"], 1) if row["want_n"] else None,
            "max_want": row["max_want"],
            "samples": " ⏐ ".join(row["_samples"]),
        })

    path = _store.write_vocab_csv(rows, COLUMNS, root)
    return {
        "items": rows,
        "csv": str(path),
        "titles": total,
        "note": "人工划掉不属于目标类目的词后，把保留的词写入 <picks>/keywords.txt",
    }


__test__ = {
    "_ngrams": _ngrams,
    "_prune": _prune,
}
