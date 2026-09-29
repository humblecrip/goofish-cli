"""pick evolve 的核心逻辑：权重驱动的词库组合管理。

要解决的真实约束：**词库不能无限长大**。单会话累计约 73 次 mtop 调用就触发 RGV587，
而 60 个词 × (1 页搜索 + 1 次供给量查询) = 120 次调用，必然触墙。所以「动态更新词库」
必须同时回答两个问题：

  1. 词库里该有哪些词（长期组合）—— 按打分与权重增删，带迟滞防抖；
  2. 本轮该采哪些词（周期子集）—— 从组合里按分数选出一个**预算装得下的子集**，
     并把长期没被采到的尾部词轮换进来，否则它们永远拿不到数据、永远无法被评分。

三条设计原则：
  - **迟滞**：只有连续 N 轮都排在末尾才淘汰，避免"加了又删"来回抖动；
  - **有据可查**：每次增删都写 changelog（词、动作、原因、当时的分数/依据）；
  - **默认不写**：命令默认 dry-run，只有显式 --apply 才改词库。
"""
from __future__ import annotations

from typing import Any

# 子串匹配：履约/交易样板词 —— 它们绝不会是品类名的一部分
DEFAULT_STOPWORDS: tuple[str, ...] = (
    # 履约/交易词：几乎每条标题都有，不构成品类信号（实测占 vocab 榜首）
    "发货", "秒发", "自动", "拍下", "链接", "网盘", "不退", "付款", "下单", "购买",
    "标价", "包邮", "现货", "秒到", "即买", "直发", "极速", "全天", "24小时", "小时",
    # 泛化指代
    "支持", "需要", "可以", "都有", "适合", "包含", "使用", "直接", "内容", "商品",
    "系统", "基础", "最新", "虚拟", "整理", "全套", "包括", "提供", "确保", "注意",
    "不是", "不要", "完全", "长期", "稳定", "永久", "独家", "超清", "高清", "通用",
    # 平台/渠道名
    "百度", "夸克", "阿里", "微信", "手机", "电脑", "设备", "平台", "软件",
    # 单字与无意义拉丁片段
    "in", "of", "to", "and", "the", "vip", "svip",
)

# 仅精确匹配：泛化词。作为**片段**时可能是合法品名的一部分，
# 例如「软件」不该否决「软件安装包」、「使用」不该否决「使用教程」。
DEFAULT_STOPWORDS_EXACT: tuple[str, ...] = (
    "自动", "支持", "需要", "可以", "都有", "适合", "包含", "使用", "直接", "内容",
    "商品", "系统", "基础", "最新", "虚拟", "整理", "全套", "包括", "提供", "确保",
    "注意", "不是", "不要", "完全", "长期", "稳定", "永久", "独家", "超清", "高清",
    "通用", "百度", "夸克", "阿里", "微信", "手机", "电脑", "设备", "平台", "软件",
)


def _norm(term: str) -> str:
    return term.strip().lower()


def is_valid_term(term: str, cfg: dict[str, Any]) -> tuple[bool, str]:
    """判断一个候选片段是否值得进入候选池（不是最终入选，只是筛掉明显噪声）。"""
    t = _norm(term)
    if len(t) < 2:
        return False, "长度不足 2"

    subs = [_norm(w) for w in (cfg.get("stopwords") or DEFAULT_STOPWORDS)]
    hit = next((w for w in subs if w and w in t), None)
    if hit:
        return False, f"履约样板词「{hit}」（子串命中）"

    exact = {_norm(w) for w in (cfg.get("stopwords_exact") or DEFAULT_STOPWORDS_EXACT)}
    if t in exact:
        return False, "泛化词（整词命中）"

    exclusion = cfg.get("exclusion") or {}
    for kind, words in (("人工介入型", exclusion.get("human")),
                        ("续费型", exclusion.get("renewal_strong")),
                        ("续费型", exclusion.get("renewal_weak"))):
        for word in words or []:
            if word and _norm(word) in t:
                return False, f"{kind}关键词「{word}」"

    if t.isdigit():
        return False, "纯数字"
    return True, ""


def promote_candidates(candidates: list[dict[str, Any]], existing: list[str],
                       cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """从词频候选里挑出值得加入词库的新词。

    `candidates` 形如 pick vocab 的输出行（term/coverage/titles/avg_want）。
    `existing` 是当前词库；已被现有词覆盖的片段不再重复引入。
    """
    evolve = cfg.get("evolve") or {}
    min_titles = int(evolve.get("min_term_titles") or 8)
    min_coverage = float(evolve.get("min_term_coverage") or 0.02)
    max_promote = int(evolve.get("max_promote_per_cycle") or 6)
    max_coverage = float(evolve.get("max_term_coverage") or 0.12)
    per_cycle = int(evolve.get("new_words_per_cycle") or 3)

    existing_norm = {_norm(k) for k in existing}
    out: list[dict[str, Any]] = []
    for row in candidates:
        term = str(row.get("term") or "")
        ok, reason = is_valid_term(term, cfg)
        if not ok:
            continue
        t = _norm(term)
        if t in existing_norm:
            continue
        # 与现有词高度重叠的片段不再引入（「考研资料」已存在时不再加「考研」）
        if any(t in e or e in t for e in existing_norm if len(e) >= 2):
            continue
        if int(row.get("titles") or 0) < min_titles:
            continue
        if float(row.get("coverage") or 0) < min_coverage:
            continue
        # 覆盖率过高的词是样板话术或过泛词（如「发货」37%、「模板」19%），
        # 当独立搜索词没有价值；真正可用的品类名往往是**具体**的，覆盖率反而低。
        # 这一步是本方案里唯一被数据验证有效的候选过滤条件。
        if float(row.get("coverage") or 0) > max_coverage:
            continue
        # 注：曾尝试用「词边界率」区分真词与碎片，但实测无效 —— 语料里碎片的边界率
        # （0.35-0.86）与好词（0.38-0.84）完全重叠，因为标题本身是关键词堆砌、
        # 人为把词放在边界上。该列仍输出供人工参考，但不作为过滤条件。
        out.append({
            "term": term,
            "titles": row.get("titles"),
            "coverage": row.get("coverage"),
            "avg_want": row.get("avg_want"),
            # 新词还没有采集数据，用「出现广度 × 平均需求」当代理分排序
            "proxy": round(float(row.get("avg_want") or 0) * float(row.get("titles") or 0), 1),
            "reason": (f"新候选：覆盖 {row.get('coverage')} / {row.get('titles')} 条 / "
                       f"均想要 {row.get('avg_want')} / 边界率 {row.get('boundary_rate')}"),
        })

    out.sort(key=lambda r: -r["proxy"])
    return out[:min(max_promote, per_cycle)]


def select_table(existing: list[str], promoted: list[dict[str, Any]],
                 scores: dict[str, float], state: dict[str, Any],
                 cfg: dict[str, Any]) -> dict[str, Any]:
    """决定词库的最终成员，并给出本轮采集子集。

    返回 {"keep": [...], "add": [...], "retire": [...], "cycle": [...]}。
    """
    evolve = cfg.get("evolve") or {}
    max_keywords = int(evolve.get("max_keywords") or 60)
    retire_bottom = int(evolve.get("retire_bottom") or 5)
    retire_streak = int(evolve.get("retire_streak") or 2)
    cycle_words = int(evolve.get("cycle_words") or 25)
    rotate_slots = int(evolve.get("rotate_slots") or 3)
    min_keywords = int(evolve.get("min_keywords") or 10)

    history: dict[str, Any] = (state.get("keywords") or {})
    cycle_id = int(state.get("cycle") or 0) + 1

    # 按分数排序（未采过的词没有分数，排在已有分数词之后、但优先于长期低分词）
    scored = [(k, scores.get(k)) for k in existing]
    known = sorted([(k, s) for k, s in scored if isinstance(s, (int, float))],
                   key=lambda kv: -kv[1])
    unknown = [k for k, s in scored if not isinstance(s, (int, float))]

    # 下限保护：词库规模不超过下限时**完全不做末尾淘汰**。
    # 否则「词库 2 个词 + retire_bottom 2」会让每个词都在末尾，两轮后整库清空——
    # 这是我第一版的实际行为，被测试抓住。
    removable = max(0, len(known) - min_keywords)
    allow_retire = retire_bottom > 0 and removable > 0
    bottom = {k for k, _ in known[-min(retire_bottom, removable):]} if allow_retire else set()

    retired: list[dict[str, Any]] = []
    keep: list[str] = []
    for keyword in existing:
        score = scores.get(keyword)
        entry = history.setdefault(keyword, {})
        if keyword in bottom:
            entry["low_streak"] = int(entry.get("low_streak") or 0) + 1
        else:
            entry["low_streak"] = 0
        entry["last_score"] = score
        if entry["low_streak"] >= retire_streak:
            retired.append({
                "term": keyword,
                "reason": f"连续 {entry['low_streak']} 轮排在末尾 {retire_bottom} 名（分数 {score}）",
            })
            history.pop(keyword, None)
            continue
        keep.append(keyword)

    added = [{"term": row["term"], "reason": row["reason"], "proxy": row["proxy"]}
             for row in promoted]

    # 词库上限：先保高分老词，再用新候选补位
    keep_sorted = [k for k, _ in known if k in keep] + [k for k in unknown if k in keep]
    room = max(0, max_keywords - len(added))
    keep_final = keep_sorted[:room]
    overflow = keep_sorted[room:]
    for keyword in overflow:
        retired.append({"term": keyword, "reason": f"超出词库上限 {max_keywords}"})
        history.pop(keyword, None)

    final = keep_final + [row["term"] for row in added]

    # 本轮采集子集：高分优先，但**必须预留槽位**给长期未采到的尾部词。
    # 预留是关键：不预留的话，高分词会永久填满子集，尾部词永远拿不到数据、
    # 永远无法被打分，于是"轮换"形同虚设（我第一版就是这么写的，被测试抓住）。
    def _last_collected(term: str) -> int:
        return int((history.get(term) or {}).get("last_collected") or 0)

    by_score = sorted(final, key=lambda k: (not isinstance(scores.get(k), (int, float)),
                                            -(scores.get(k) or 0)))
    reserved = max(0, min(rotate_slots, cycle_words // 2))
    cycle = by_score[:cycle_words - reserved]

    starved = sorted((k for k in final if k not in cycle), key=_last_collected)
    for keyword in starved:
        if len(cycle) >= cycle_words:
            break
        if cycle_id - _last_collected(keyword) >= 2:      # 至少两轮没采过 → 轮换进来
            cycle.append(keyword)

    for keyword in by_score:                              # 没有饥饿词时用分数补满
        if len(cycle) >= cycle_words:
            break
        if keyword not in cycle:
            cycle.append(keyword)

    for keyword in cycle:
        history.setdefault(keyword, {})["last_collected"] = cycle_id

    return {
        "keep": final,
        "add": added,
        "retire": retired,
        "cycle": cycle,
        "cycle_id": cycle_id,
        "state": {"cycle": cycle_id, "keywords": history},
    }
