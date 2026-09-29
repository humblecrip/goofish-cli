"""搜索接口封装与商品字段解析。

字段路径全部在真实响应上验证过（见 goofish-cli 仓库 issue/文档），关键坑：

  - `clickParam.args.wantNum` **恒为 "0"**、`exContent.want` **恒为 ""** —— 都是埋点
    占位符，真实「想要人数」在 `exContent.fishTags.*.tagList[*].data.content` 里，
    形如 "142人想要"，只能正则提取。
  - 卖家「成交基数」（N条评价）与「好评率」在 `exContent.userFishShopLabel.tagList`
    的 content 里，同样是文案而非数值字段。
  - 卖家 uid 只能从 `userAvatarUrl` 的 `!!<uid>-` 段提取，覆盖率约 70-85%
    （`!!0-` 或默认头像时缺失）。
  - `price` 是富文本节点列表（`[{"text":"¥"},{"text":"9"},{"text":".9"}]`），
    不是数字。
  - 供给量 `hitnum` 服务端截断在 800000：`女装`/`手机` 之类宽词恒返回该值，
    只有低于它才可信。
"""
from __future__ import annotations

import re
from typing import Any

from goofish_cli.core.mtop import call

SEARCH_API = "mtop.taobao.idlemtopsearch.pc.search"
SUPPLY_API = "mtop.taobao.idle.filter.hitnum.pc.get"
RATE_API = "mtop.idle.web.trade.rate.list"
SEARCH_SPM = "a21ybx.search.0.0"
USER_SPM = "a21ybx.user.0.0"

SUPPLY_CAP = 800_000
DEFAULT_ROWS = 30

# 与首页搜索下拉一致的 7 种排序。**没有按销量排序**——平台不提供。
SORTS: dict[str, tuple[str, str]] = {
    "综合": ("", ""),
    "价格升序": ("price", "asc"),
    "价格降序": ("price", "desc"),
    "新降价": ("reduce", "desc"),
    "修改时间": ("modify", "desc"),
    "信用": ("credit", "credit_desc"),
    "距离": ("pos", "asc"),
}

_UID_RE = re.compile(r"!!(\d+)-")
_WANT_RE = re.compile(r"(\d+)\s*人想要")
_RATE_RE = re.compile(r"(\d+)\s*条评价")
_GOOD_RE = re.compile(r"好评率\s*(\d+)\s*%")


def _scan_contents(node: Any) -> str:
    """把嵌套结构里所有 content 字段拼成一个串（用于文案型字段的正则提取）。"""
    out: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, val in obj.items():
                if key == "content" and isinstance(val, str):
                    out.append(val)
                else:
                    walk(val)
        elif isinstance(obj, list):
            for val in obj:
                walk(val)

    walk(node)
    return " | ".join(out)


def _rich_text(node: Any) -> str:
    """富文本节点 → 纯文本。price 就是这种结构。"""
    if isinstance(node, list):
        return "".join(str(n.get("text", "")) for n in node if isinstance(n, dict)).strip()
    if isinstance(node, dict):
        return str(node.get("content") or node.get("text") or "").strip()
    return str(node or "").strip()


def search_page(session: Any, keyword: str, page: int = 1, rows: int = DEFAULT_ROWS,
                sort: str = "综合") -> list[dict[str, Any]]:
    """取一页搜索结果（原始 resultList）。"""
    field, value = SORTS.get(sort, ("", ""))
    raw = call(
        session,
        api=SEARCH_API,
        data={
            "pageNumber": page,
            "keyword": keyword,
            "fromFilter": bool(field),
            "rowsPerPage": rows,
            "sortValue": value,
            "sortField": field,
            "customDistance": "",
            "gps": "",
            "propValueStr": {},
            "customGps": "",
            "searchReqFromPage": "pcSearch",
            "extraFilterValue": "",
            "userPositionJson": "",
        },
        version="1.0",
        spm_cnt=SEARCH_SPM,
    )
    return (raw.get("data") or {}).get("resultList") or []


def parse_item(row: dict[str, Any], keyword: str) -> dict[str, Any] | None:
    """resultList[i] → 扁平记录。结构不认识时返回 None（跳过而不是抛错）。"""
    try:
        main = row["data"]["item"]["main"]
        ex = main.get("exContent") or {}
        args = (main.get("clickParam") or {}).get("args") or {}
    except (KeyError, TypeError):
        return None

    item_id = str(ex.get("itemId") or args.get("item_id") or "")
    if not item_id:
        return None

    want: int | None = None
    matched = _WANT_RE.search(_scan_contents(ex.get("fishTags")))
    if matched:
        want = int(matched.group(1))

    shop_blob = _scan_contents(ex.get("userFishShopLabel"))
    rate_hit, good_hit = _RATE_RE.search(shop_blob), _GOOD_RE.search(shop_blob)
    rate_count = int(rate_hit.group(1)) if rate_hit else None
    good_rate = int(good_hit.group(1)) if good_hit else None

    avatar = ex.get("userAvatarUrl") or ""
    uid_hit = _UID_RE.search(avatar)
    seller_uid = uid_hit.group(1) if uid_hit and uid_hit.group(1) != "0" else None

    publish_ts: int | None = None
    try:
        publish_ts = int(args["publishTime"]) if args.get("publishTime") else None
    except (TypeError, ValueError):
        publish_ts = None

    price_txt = _rich_text(ex.get("price"))
    clean_price = price_txt.replace("¥", "").strip()
    try:
        price_num: float | None = float(clean_price)
    except ValueError:
        price_num = None            # 「面议」「区间」等非数值

    return {
        "item_id": item_id,
        "keyword": keyword,
        "title": (ex.get("title") or "").strip(),
        "price": f"¥{price_num:g}" if price_num is not None else price_txt,
        "price_num": price_num,
        "ori_price": (ex.get("oriPrice") or "").strip(),
        "area": (ex.get("area") or "").strip(),
        "want": want,
        "seller_uid": seller_uid,
        "seller_nick": (ex.get("userNickName") or "").strip(),
        "seller_rate_count": rate_count,
        "seller_good_rate": good_rate,
        "publish_ts": publish_ts,
        "cat_id": args.get("catId") or "",
        "c_cat_id": args.get("cCatId") or "",
        "tb_cat_id": args.get("tbCatId") or "",
        "tag": args.get("tag") or "",
        "item_type": args.get("item_type") or "",
    }


def supply_count(session: Any, keyword: str) -> int | None:
    """关键词下的商品总数。返回值 >= SUPPLY_CAP 表示「被截断，至少这么多」。"""
    raw = call(
        session,
        api=SUPPLY_API,
        data={"keyword": keyword},
        version="1.0",
        spm_cnt=SEARCH_SPM,
    )
    value = (raw.get("data") or {}).get("hitnum")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def seller_rates(session: Any, uid: str, rows: int = 20) -> dict[str, Any] | None:
    """卖家的逐笔成交时间线与累计成交数。

    ⚠️ 仅普通号有效：`46116860...` 这类店铺号实测 `totalCount` 恒为 0，
    此时返回 None（属正常，不是错误）。
    """
    raw = call(
        session,
        api=RATE_API,
        data={
            "rateType": 0,
            "ratedUid": str(uid),
            "raterType": 0,
            "rowsPerPage": rows,
            "pageNumber": 1,
            "foldFlag": False,
            "fishAdCode": "330110",
            "extraTag": "",
        },
        version="1.0",
        spm_cnt=USER_SPM,
    )
    data = raw.get("data") or {}
    total = data.get("totalCount")
    if not total:
        return None
    orders = []
    for card in data.get("cardList") or []:
        card_data = card.get("cardData") or {}
        if card_data.get("gmtCreate"):
            orders.append({"gmt": card_data["gmtCreate"], "rate": card_data.get("rate")})
    return {"total": int(total), "orders": orders}
