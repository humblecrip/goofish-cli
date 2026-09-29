"""pick rank — 排除过滤 → 6 维分位打分 → 出榜。

产出四份东西（全部冻结落盘，含本次使用的权重，便于事后归因）：
  - `reports/<ts>.json`            完整结果（含权重来源、快照清单、降级维度）
  - `reports/<ts>-categories.csv`  类目榜（主结论，也是 stdout 渲染的内容）
  - `reports/<ts>-sellers.csv`     卖家榜（谁在做、怎么做的）
  - `reports/<ts>-items.csv`       商品明细 + 被过滤标记与原因（被过滤清单的载体）

快照龄超过阈值（默认 12h）时会**自动补采一轮**——成交速率必须有 ≥2 个不同时刻的
快照才能算，而信号周期是「天」，几小时内重复采集不会产生新信息。`--no-collect` 可关闭。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from goofish_cli.commands.pick import _scoring, _store
from goofish_cli.core import Strategy, command
from goofish_cli.core.errors import GoofishError

CATEGORY_COLUMNS = [
    "rank", "score", "keyword", "supply", "supply_capped", "samples",
    "want_median", "want_sum", "seller_rate_median", "delta_rate_median", "price_median",
    "excluded_ratio", "top_cat_ids",
    "imbalance_raw", "imbalance_norm",
    "delta_rate_raw", "delta_rate_norm",
    "want_raw", "want_norm",
    "seller_base_raw", "seller_base_norm",
    "samples_raw", "samples_norm",
    "price_raw", "price_norm",
    "dims_used", "dims_degraded",
]

SELLER_COLUMNS = [
    "rank", "seller", "uid", "delta_rate", "rate_count", "good_rate",
    "items_sampled", "price_median", "keywords",
]

ITEM_COLUMNS = [
    "item_id", "keyword", "keywords", "title", "price", "price_num", "area", "want",
    "seller_nick", "seller_uid", "seller_rate_count", "seller_good_rate",
    "publish_ts", "cat_id", "c_cat_id", "tb_cat_id", "tag",
    "excluded", "exclude_reason",
]


def _may_collect(root: Path | None, threshold: float, enabled: bool) -> dict[str, Any] | None:
    """快照过期则补采一轮。返回采集结果；不需要/失败时不阻塞出榜。"""
    if not enabled:
        return None
    age = _store.snapshot_age_hours(root)
    if age is not None and age <= threshold:
        return None
    from goofish_cli.commands.pick.collect import collect as collect_cmd

    try:
        return collect_cmd()
    except GoofishError as exc:            # 采集失败不该让出榜也失败
        return {"aborted": f"自动补采失败：{type(exc).__name__}: {str(exc)[:120]}"}


def write_xlsx(payload: dict[str, Any], categories: list[dict[str, Any]],
               sellers: list[dict[str, Any]], items: list[dict[str, Any]],
               root: Path | None = None, name: str | None = None) -> Path:
    """把一轮结果写成一个多 sheet 工作簿。

    openpyxl 是**可选依赖**（`pip install 'goofish-cli[excel]'`）——不装的话
    CSV/JSON 照常可用，只有加 --xlsx 时才会提示。
    """
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise GoofishError(
            "需要 openpyxl 才能输出 xlsx：pip install 'goofish-cli[excel]'"
            "（不加 --xlsx 时 CSV/JSON 不受影响）"
        ) from exc

    excluded = [row for row in items if row.get("excluded")]
    sheets: list[tuple[str, list[dict[str, Any]], list[str]]] = [
        ("类目榜", categories, CATEGORY_COLUMNS),
        ("卖家榜", sellers, SELLER_COLUMNS),
        ("商品明细", items, ITEM_COLUMNS),
        ("被过滤", excluded, ITEM_COLUMNS),
    ]

    wb = Workbook()
    # 概览页：让打开工作簿的人先知道「这份数据是怎么来的、当时用的什么权重」
    overview = wb.active
    overview.title = "概览"
    overview.append(["字段", "值"])
    overview.append(["生成时间", str(payload.get("generated_at", ""))])
    overview.append(["权重来源", str(payload.get("weights_source", ""))])
    for dim, cfg in (payload.get("weights_used", {}).get("dimensions") or {}).items():
        overview.append([f"权重·{dim}", cfg.get("weight")])
    counts = payload.get("counts") or {}
    for key in ("categories", "sellers", "items", "excluded"):
        overview.append([f"条数·{key}", counts.get(key)])
    overview.append(["快照文件数", len(payload.get("snapshots") or [])])

    head_font = Font(bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor="4472C4")
    for title, rows, columns in sheets:
        ws = wb.create_sheet(title)
        ws.append(columns)
        for row in rows:
            ws.append([_store.cell_text(row.get(col)) for col in columns])
        for cell in ws[1]:
            cell.font = head_font
            cell.fill = head_fill
            cell.alignment = Alignment(horizontal="center")
        ws.freeze_panes = "A2"                      # 冻结首行，滚动时表头常驻
        for idx, col in enumerate(columns, 1):
            width = max(len(col), *(len(str(_store.cell_text(row.get(col)))[:40])
                                     for row in rows[:200])) \
                if rows else len(col)
            ws.column_dimensions[get_column_letter(idx)].width = min(max(width + 2, 8), 42)

    if root is not None:
        _store.ensure_dirs(root)
        path = (root / "reports") / f"{name or _store.stamp()}.xlsx"
    else:
        _store.ensure_dirs(None)
        path = (_store.ROOT / "reports") / f"{name or _store.stamp()}.xlsx"
    wb.save(path)
    return path


def _cat_ids(recs: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for rec in recs:
        for key in ("cat_id", "c_cat_id"):
            value = str(rec.get(key) or "")
            if value:
                counts[value] = counts.get(value, 0) + 1
    top = sorted(counts.items(), key=lambda kv: -kv[1])[:3]
    return " | ".join(f"{cid}:{n}" for cid, n in top)


def _build(root: Path | None, weights: dict[str, Any]) -> dict[str, Any]:
    obs = _store.load_observations(root)
    exclusion = weights.get("exclusion") or {}
    min_samples = int(weights.get("min_samples") or 0)

    seller_delta: dict[str, float | None] = {
        key: _store.rate_per_day(series, "rate_count")
        for key, series in obs["seller"].items()
    }

    items: list[dict[str, Any]] = []
    for series in obs["item"].values():
        rec = dict(_store.latest(series))
        excluded, reason = _scoring.classify(rec, exclusion)
        rec["excluded"] = excluded
        rec["exclude_reason"] = reason
        rec["keywords"] = list(rec.get("keywords") or ([rec["keyword"]] if rec.get("keyword") else []))
        items.append(rec)

    kept = [rec for rec in items if not rec["excluded"]]

    # ── 类目榜（关键词为行）──────────────────────────────────────────────
    keywords: list[str] = []
    for rec in items:
        for kw in rec["keywords"]:
            if kw not in keywords:
                keywords.append(kw)

    categories: list[dict[str, Any]] = []
    for kw in keywords:
        members = [rec for rec in kept if kw in rec["keywords"]]
        all_members = [rec for rec in items if kw in rec["keywords"]]
        if not all_members:
            continue
        kw_row = _store.latest(obs["keyword"].get(kw) or [(None, {})])
        supply = kw_row.get("supply")
        capped = bool(isinstance(supply, int) and supply >= 800_000)
        effective_supply = None if capped else supply

        wants = [r["want"] for r in members if isinstance(r.get("want"), (int, float))]
        prices = [r["price_num"] for r in members if isinstance(r.get("price_num"), (int, float))]
        bases = [r["seller_rate_count"] for r in members
                 if isinstance(r.get("seller_rate_count"), (int, float))]
        deltas = [seller_delta.get(r.get("seller_uid") or f"nick:{r.get('seller_nick')}")
                  for r in members]
        deltas = [d for d in deltas if isinstance(d, (int, float))]

        want_median = _scoring.median(wants)
        categories.append({
            "keyword": kw,
            "supply": supply,
            "supply_capped": capped,
            "samples": len(members),
            "min_samples_ok": len(members) >= min_samples,
            "want_median": want_median,
            "want_sum": sum(wants) if wants else None,
            "seller_rate_median": _scoring.median(bases),
            "delta_rate_median": _scoring.median(deltas),
            "price_median": _scoring.median(prices),
            "excluded_ratio": round(
                (len(all_members) - len(members)) / len(all_members), 3),
            "top_cat_ids": _cat_ids(members),
            "want_raw": want_median,
            "seller_base_raw": _scoring.median(bases),
            "samples_raw": len(members),
            "price_raw": _scoring.median(prices),
            "delta_rate_raw": _scoring.median(deltas),
            "imbalance_raw": (want_median / effective_supply)
            if (want_median is not None and effective_supply) else None,
        })

    _scoring.add_scores(categories, weights)
    categories.sort(key=lambda r: (-(r.get("score") or 0), -(r.get("samples") or 0)))
    for i, row in enumerate(categories, 1):
        row["rank"] = i

    # ── 卖家榜 ──────────────────────────────────────────────────────────
    sellers: list[dict[str, Any]] = []
    for key, series in obs["seller"].items():
        rec = dict(_store.latest(series))
        if rec.get("rate_count") is None:
            continue
        kws = [kw for kw in rec.get("keywords") or [] if kw in keywords]
        if not kws:
            continue
        nick = rec.get("nick") or ""
        owned = [r["price_num"] for r in kept
                 if r.get("seller_uid") == rec.get("uid") or r.get("seller_nick") == nick]
        rec["seller"] = nick
        rec["delta_rate"] = seller_delta.get(key)
        rec["price_median"] = _scoring.median([p for p in owned
                                               if isinstance(p, (int, float))])
        sellers.append(rec)
    sellers.sort(key=lambda r: (r.get("delta_rate") is None,
                               -(r.get("delta_rate") or 0),
                               -(r.get("rate_count") or 0)))
    for i, row in enumerate(sellers, 1):
        row["rank"] = i

    return {
        "categories": categories,
        "sellers": sellers,
        "items": sorted(items, key=lambda r: -(r.get("want") or 0)),
        "snapshots": [str(path) for _, path in obs["snapshots"]],
    }


@command(
    namespace="pick",
    name="rank",
    description="选品榜：排除过滤 + 6 维分位打分（类目榜 / 卖家榜 / 被过滤清单）",
    strategy=Strategy.COOKIE,
    columns=CATEGORY_COLUMNS,
)
def rank(
    out: str = "",
    weights: str = "",
    top: int = 20,
    no_collect: bool = False,
    xlsx: bool = False,
) -> dict[str, Any]:
    """`--weights` 可传 JSON 串或文件路径，临时覆盖用户权重文件。"""
    root = Path(out).expanduser() if out else None
    _store.ensure_dirs(root)
    cfg, cfg_source = _store.load_weights(root, weights or None)
    threshold = float(cfg.get("snapshot_max_age_hours") or 12)
    collected = _may_collect(root, threshold, not no_collect)

    result = _build(root, cfg)
    stamp = _store.stamp()
    payload = {
        "generated_at": _store.now_iso(),
        "weights_source": cfg_source,
        "weights_used": cfg,
        "collected": collected,
        "snapshots": result["snapshots"],
        "counts": {
            "categories": len(result["categories"]),
            "sellers": len(result["sellers"]),
            "items": len(result["items"]),
            "excluded": sum(1 for r in result["items"] if r["excluded"]),
        },
        "categories": result["categories"][:max(1, top)],
        "sellers": result["sellers"][:max(1, top)],
        "items": result["items"],
    }
    xlsx_path = None
    if xlsx:
        xlsx_path = write_xlsx(payload, result["categories"], result["sellers"],
                               result["items"], root, name=stamp)

    json_path = _store.write_json(payload, root, name=stamp)
    csv_paths = [
        _store.write_csv(result["categories"], CATEGORY_COLUMNS, root, f"{stamp}-categories"),
        _store.write_csv(result["sellers"], SELLER_COLUMNS, root, f"{stamp}-sellers"),
        _store.write_csv(result["items"], ITEM_COLUMNS, root, f"{stamp}-items"),
    ]

    return {
        "categories": result["categories"],
        "sellers": result["sellers"],
        "items": result["items"],
        "meta": {
            "report": str(json_path),
            "csv": [str(p) for p in csv_paths],
            "xlsx": str(xlsx_path) if xlsx_path else None,
            "weights_source": cfg_source,
            "snapshots": len(result["snapshots"]),
            "collected": collected,
            "counts": payload["counts"],
        },
    }


__test__ = {
    "_cat_ids": _cat_ids,
    "_build": _build,
}
