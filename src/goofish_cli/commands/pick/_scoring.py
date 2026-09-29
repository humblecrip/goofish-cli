"""选品判据：排除规则 + 6 维分位打分。

两条硬约束来自真实数据，不是设计偏好：

1. 「想要人数」是**发布以来累计值**，老品天然占优 —— 单看它会把"挂了三年的链接"
   排到"最近爆发的新品"前面。所以它只作原始输入之一，真正的判据是**需求供给失衡**
   （想要 ÷ 有效供给）与**成交速率**（Δ评价数/天）。
2. 「某商品卖了多少」平台不提供（`trade.rate.list` 的 `itemId`/`itemPrice` 恒为 0），
   所以只出**类目级**与**卖家级**结论，不做商品级销量结论。

排除规则采用「强/弱续费词」分层（见 weights.default.json 的 exclusion 段）：
  - 强续费词命中即排除（它们本身就是服务型商品的强标识）
  - 弱续费词（如「会员」「vip」）**只在价格也低时**才排除 —— 因为「会员」这个词
    也可能出现在素材/资料标题里，单靠它会误杀
  - 人工介入词命中即排除（代练/陪玩/一对一/定制…都需要卖家持续投入时间）
"""
from __future__ import annotations

from typing import Any

DIMENSIONS: tuple[str, ...] = (
    "imbalance", "delta_rate", "want", "seller_base", "samples", "price",
)

DIM_LABELS: dict[str, str] = {
    "imbalance": "需求供给失衡",
    "delta_rate": "成交速率",
    "want": "需求热度",
    "seller_base": "卖家成交基数",
    "samples": "样本可信度",
    "price": "价格带",
}

_DEFAULT_EXCLUSION: dict[str, Any] = {
    "renewal_strong": [],
    "renewal_weak": [],
    "human": [],
    "low_price_ceiling": 5.0,
}


def _hit(title: str, words: list[str]) -> str | None:
    lowered = title.lower()
    for word in words:
        if word and word.lower() in lowered:
            return word
    return None


def classify(rec: dict[str, Any], exclusion: dict[str, Any] | None = None) -> tuple[bool, str]:
    """判断一条商品是否应被排除，返回 (excluded, 原因)。原因会进被过滤清单。"""
    cfg = {**_DEFAULT_EXCLUSION, **(exclusion or {})}
    title = str(rec.get("title") or "")

    human = _hit(title, list(cfg.get("human") or []))
    if human:
        return True, f"人工介入型：命中「{human}」"

    strong = _hit(title, list(cfg.get("renewal_strong") or []))
    if strong:
        return True, f"续费型：命中「{strong}」"

    weak = _hit(title, list(cfg.get("renewal_weak") or []))
    if weak:
        price = rec.get("price_num")
        ceiling = cfg.get("low_price_ceiling")
        if isinstance(price, (int, float)) and isinstance(ceiling, (int, float)) and price <= ceiling:
            return True, f"续费型组合：命中弱词「{weak}」且价格 ¥{price:g} ≤ ¥{ceiling:g}"
        return False, ""

    return False, ""


def quantile_scores(values: list[float | None]) -> list[float | None]:
    """组内百分位（0-100）。None 保持 None；有效样本 <2 时记 50（中性）。

    为什么用分位而不是固定区间：闲鱼的量级波动极大，且我们没有历史基准可以参照。
    固定区间一旦猜错，结果会全部挤在 60-70 分、失去分辨力；分位永远不会因为
    绝对值猜错而失效。代价是分数不能跨轮比较（这一轮的第 80 分与下一轮无关）。
    """
    idx = [i for i, v in enumerate(values) if v is not None]
    out: list[float | None] = [None] * len(values)
    if not idx:
        return out
    if len(idx) < 2:
        for i in idx:
            out[i] = 50.0
        return out

    order = sorted(idx, key=lambda i: values[i])
    total = len(order)
    pos = 0
    while pos < total:
        end = pos
        while end + 1 < total and values[order[end + 1]] == values[order[pos]]:
            end += 1
        pct = round((pos + end) / 2 / (total - 1) * 100, 1)
        for k in range(pos, end + 1):
            out[order[k]] = pct
        pos = end + 1
    return out


def add_scores(rows: list[dict[str, Any]], weights: dict[str, Any]) -> None:
    """就地补上每个维度的归一化分与加权总分。

    权重来自配置（可改可回滚）；某维度缺数据（如单快照时的 `delta_rate`）时
    该维度**退出加权并按剩余权重重新归一化**，而不是当 0 分参与 —— 否则会把
    分数拖成一个假的低值。
    """
    dims_cfg: dict[str, Any] = weights.get("dimensions") or {}
    names = [n for n in DIMENSIONS if n in dims_cfg] or list(DIMENSIONS)

    for name in names:
        raw_key = f"{name}_raw"
        norms = quantile_scores([r.get(raw_key) for r in rows])
        for row, norm in zip(rows, norms, strict=True):
            row[f"{name}_norm"] = norm

    for row in rows:
        numerator = denominator = 0.0
        used: list[str] = []
        degraded: list[str] = []
        for name in names:
            norm = row.get(f"{name}_norm")
            weight = float((dims_cfg.get(name) or {}).get("weight") or 0)
            if norm is None or weight <= 0:
                if norm is None:
                    degraded.append(name)
                continue
            numerator += norm * weight
            denominator += weight
            used.append(name)
        row["score"] = round(numerator / denominator, 1) if denominator else None
        row["dims_used"] = ",".join(used)
        row["dims_degraded"] = ",".join(degraded)


def median(values: list[float]) -> float | None:
    nums = [v for v in values if isinstance(v, (int, float))]
    if not nums:
        return None
    nums.sort()
    mid = len(nums) // 2
    if len(nums) % 2:
        return float(nums[mid])
    return (nums[mid - 1] + nums[mid]) / 2
