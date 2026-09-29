"""pick collect — 采集商品快照。

三层护栏（全部来自实测，不是设计偏好）：
  1. **单轮调用预算**（`run_budget`）—— 实测单会话累计约 250 次 mtop 调用即触发
     RGV587，所以要能「跑到额度就停」，而不是撞墙才停；
  2. **撞风控即停** —— 触发后继续请求会加深封锁（实测触发后又打 11 个词全废）；
  3. **可选等冷却续跑**（`--wait-cooldown`）—— 冷却实测 12-14 分钟，等待后用
     同一页重试，而不是跳页（跳页会静默丢数据）。

部分结果照样落盘；`pick rank` 会合并目录下所有快照，所以分多轮跑天然是并集。
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from goofish_cli.commands.pick import _search, _store
from goofish_cli.core import Session, Strategy, command, guard, limiter
from goofish_cli.core.errors import GoofishError, RateLimitedError, RiskControlError

COLUMNS = ["keyword", "supply", "fetched", "secs", "error"]
MAX_COOLDOWN_WAITS = 4
MAX_PAGES = 50


def _wait_cooldown(enabled: bool, state: dict[str, Any]) -> bool:
    """撞风控后：若允许等待，睡到冷却结束并返回 True（调用方应重试同一处）。"""
    if not enabled or state["waits"] >= MAX_COOLDOWN_WAITS:
        return False
    remain = guard.remaining()
    if remain <= 0:
        return False
    state["waits"] += 1
    while remain > 0:
        time.sleep(min(remain, 30.0))
        remain = guard.remaining()
    return True


def _fetch_page(session: Any, keyword: str, page: int, rows: int, sort: str,
                run_budget: int, wait_cooldown: bool,
                state: dict[str, Any]) -> list[dict[str, Any]]:
    """取一页。风控 → 等冷却后重试；预算用尽 → 抛 RateLimitedError 交给上层停。"""
    while True:
        limiter.consume_budget(run_budget, label="采集")
        try:
            return _search.search_page(session, keyword, page=page, rows=rows, sort=sort)
        except RiskControlError as exc:
            if _wait_cooldown(wait_cooldown, state):
                continue
            state["abort"] = f"{keyword} 第{page}页撞风控：{str(exc)[:90]}"
            return []
        except GoofishError as exc:
            state["error"] = f"{type(exc).__name__}: {str(exc)[:90]}"
            return []


def _fetch_supply(session: Any, keyword: str, run_budget: int, wait_cooldown: bool,
                  state: dict[str, Any]) -> int | None:
    while True:
        limiter.consume_budget(run_budget, label="采集")
        try:
            return _search.supply_count(session, keyword)
        except RiskControlError as exc:
            if _wait_cooldown(wait_cooldown, state):
                continue
            state["abort"] = f"供给量查询撞风控：{str(exc)[:90]}"
            return None
        except GoofishError as exc:
            state["error"] = f"{type(exc).__name__}: {str(exc)[:90]}"
            return None


@command(
    namespace="pick",
    name="collect",
    description="采集商品快照（单轮调用预算 + 撞风控可等冷却续跑）",
    strategy=Strategy.COOKIE,
    columns=COLUMNS,
    arguments=["keywords"],
)
def collect(
    keywords: str = "",
    pages: int = 2,
    rows: int = _search.DEFAULT_ROWS,
    sort: str = "综合",
    out: str = "",
    run_budget: int = 60,
    wait_cooldown: bool = False,
    seller_timeline: int = 0,
) -> dict[str, Any]:
    """`keywords` 省略时用受管词表（<picks>/keywords.txt）。"""
    root = Path(out).expanduser() if out else None
    _store.ensure_dirs(root)
    table = _store.load_keywords(root, keywords or None)
    pages = max(1, min(MAX_PAGES, int(pages)))
    session = Session.load()
    ts = _store.now_iso()
    limiter.reset_budget()

    items: dict[str, dict[str, Any]] = {}
    sellers: dict[str, dict[str, Any]] = {}
    rows_out: list[dict[str, Any]] = []
    state: dict[str, Any] = {"abort": "", "error": "", "waits": 0}

    for keyword in table:
        if state["abort"]:
            break
        started = time.time()
        state["error"] = ""
        supply = _fetch_supply(session, keyword, run_budget, wait_cooldown, state)
        if state["abort"]:
            break

        fetched = 0
        for page in range(1, pages + 1):
            page_rows = _fetch_page(session, keyword, page, rows, sort,
                                    run_budget, wait_cooldown, state)
            if state["abort"] or not page_rows:
                break
            for raw in page_rows:
                rec = _search.parse_item(raw, keyword)
                if not rec:
                    continue
                rec["ts"] = ts
                prev = items.get(rec["item_id"])
                if prev is None:
                    rec["keywords"] = [keyword]
                    items[rec["item_id"]] = rec
                else:
                    # 同一商品可能命中多个关键词：累积归属，否则按词归集会漏计
                    if keyword not in prev.setdefault("keywords", []):
                        prev["keywords"].append(keyword)
                    if (rec.get("want") or 0) > (prev.get("want") or 0):
                        rec["keywords"] = prev["keywords"]
                        items[rec["item_id"]] = rec
                fetched += 1

                key = rec.get("seller_uid") or f"nick:{rec.get('seller_nick')}"
                seller = sellers.get(key)
                if seller is None:
                    sellers[key] = {
                        "type": "seller",
                        "uid": rec.get("seller_uid"),
                        "nick": rec.get("seller_nick"),
                        "rate_count": rec.get("seller_rate_count"),
                        "good_rate": rec.get("seller_good_rate"),
                        "items_sampled": 1,
                        "keywords": [keyword],
                        "ts": ts,
                    }
                else:
                    seller["items_sampled"] += 1
                    if keyword not in seller["keywords"]:
                        seller["keywords"].append(keyword)
                    if rec.get("seller_rate_count") and \
                            (seller.get("rate_count") or 0) < rec["seller_rate_count"]:
                        seller["rate_count"] = rec["seller_rate_count"]
                    if rec.get("seller_good_rate") and not seller.get("good_rate"):
                        seller["good_rate"] = rec["seller_good_rate"]

        rows_out.append({
            "keyword": keyword,
            "supply": supply,
            "fetched": fetched,
            "secs": round(time.time() - started, 1),
            "error": state["error"],
        })

    if seller_timeline > 0 and not state["abort"]:
        for seller in [s for s in sellers.values() if s.get("uid")][:seller_timeline]:
            try:
                info = _search.seller_rates(session, seller["uid"])
            except (RiskControlError, RateLimitedError):
                break
            if info:
                seller["trade_total"] = info["total"]
                seller["recent_orders"] = info["orders"][:20]

    records: list[dict[str, Any]] = [{
        "type": "run",
        "ts": ts,
        "aborted": state["abort"],
        "calls": limiter.budget_used(),
        "keywords": [row["keyword"] for row in rows_out],
    }]
    records.extend({"type": "keyword", "ts": ts, **row} for row in rows_out)
    records.extend({"type": "item", **rec} for rec in items.values())
    records.extend(sellers.values())
    path = _store.write_snapshot(records, root)

    return {
        "items": rows_out,
        "snapshot": str(path),
        "items_total": len(items),
        "sellers_total": len(sellers),
        "calls": limiter.budget_used(),
        "aborted": state["abort"],
        "waits": state["waits"],
    }


__test__ = {
    "_wait_cooldown": _wait_cooldown,
}
