"""令牌桶限流。写路径默认 1 写/分钟，读路径默认 120 读/分钟（均可配）。

写入 ~/.goofish-cli/limiter.json 做进程间共享（单机多进程场景）。

两套限流的语义不同，不要混用：
  - `check()` / `acquire()`：**拒绝式**，超限抛 RateLimitedError。用于写操作
    （写错一次成本高、不该自动重试）。
  - `wait_turn()`：**等待式**，窗口满则 sleep 等额度。用于读操作 —— 一条命令
    常连续请求多个接口（`item publish` 会依次调图片上传/类目识别/默认地址/发布），
    拒绝式会让正当命令直接失败。
"""
from __future__ import annotations

import json
import os
import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from goofish_cli.core.errors import RateLimitedError

STATE_PATH = Path.home() / ".goofish-cli" / "limiter.json"
DEFAULT_WRITE_RPM = 1


def _rpm() -> int:
    try:
        return max(1, int(os.environ.get("GOOFISH_WRITE_RPM", DEFAULT_WRITE_RPM)))
    except ValueError:
        return DEFAULT_WRITE_RPM


def _load() -> dict[str, list[float]]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save(state: dict[str, list[float]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state))


def check(bucket: str) -> None:
    """消耗一个令牌。超限抛 RateLimitedError。"""
    now = time.time()
    window = 60.0
    rpm = _rpm()
    state = _load()
    hits = [t for t in state.get(bucket, []) if now - t < window]
    if len(hits) >= rpm:
        wait = window - (now - hits[0])
        raise RateLimitedError(
            f"限流：bucket={bucket} 每 {window:.0f}s 上限 {rpm}，再等 {wait:.1f}s"
        )
    hits.append(now)
    state[bucket] = hits
    _save(state)


@contextmanager
def acquire(bucket: str):
    check(bucket)
    yield


# ── 读路径：等待式限流 ────────────────────────────────────────────────────
# 为什么读路径需要独立一套：原版只有 3 个写命令受保护，读路径（search /
# item get / list / history …）完全没有限流。实测一个会话打 ~250 次即触发
# RGV587，而触发后继续请求会加深封锁。
DEFAULT_READ_RPM = 120
READ_JITTER = 0.25

# 少数接口的独立上限。实测：idle.pc.detail 约 4-6 次即触发风控；
# idlemtopsearch.pc.search 约 57 页（≈1700 条）触发。
_READ_LIMITS: tuple[tuple[str, int], ...] = (
    ("mtop.taobao.idle.pc.detail", 3),
    ("mtop.taobao.idlemtopsearch.pc.search", 15),
    ("mtop.idle.web.trade.rate.list", 6),
)


def _read_rpm() -> int:
    """读路径全局上限（次/分钟）。设为 0 完全关闭读限流。"""
    try:
        return max(0, int(os.environ.get("GOOFISH_READ_RPM", DEFAULT_READ_RPM)))
    except ValueError:
        return DEFAULT_READ_RPM


def read_bucket(api: str) -> tuple[str, int]:
    """返回 (bucket 名, 该接口每分钟上限)。limit <= 0 表示不限。"""
    rpm = _read_rpm()
    if rpm <= 0:
        return "read:off", 0
    for prefix, limit in _READ_LIMITS:
        if api.startswith(prefix):
            return f"read:{prefix}", min(limit, rpm)
    return "read:default", rpm


def wait_turn(api: str) -> None:
    """等待式限流：窗口满则 sleep 到有额度为止，不抛异常。附抖动。

    固定间隔本身是可识别模式，所以每次请求后加一点随机延迟打散节奏。
    """
    bucket, limit = read_bucket(api)
    if limit <= 0:
        return

    window = 60.0
    now = time.time()
    state = _load()
    hits = [t for t in state.get(bucket, []) if now - t < window]

    # 分片等待：每片最多 30s（保持可中断），之后**重新检查**窗口是否真的腾出额度。
    # 不能只等一片就放行 —— 那会让等待被 30s 上限截断后直接越过限流。
    while len(hits) >= limit:
        wait = window - (now - min(hits)) + random.uniform(0, READ_JITTER)
        time.sleep(min(max(wait, 0.05), 30.0))
        now = time.time()
        hits = [t for t in hits if now - t < window]

    hits.append(now)
    state[bucket] = hits
    _save(state)
    time.sleep(random.uniform(0, READ_JITTER * 0.2))


# ── 单轮调用预算 ─────────────────────────────────────────────────────────
# 与滑动窗口的分工：滑窗限制「每分钟多少次」，预算限制「整轮一共多少次」。
# 实测单会话累计约 250 次 mtop 调用即触发 RGV587，且触墙后继续请求会加深封锁，
# 所以需要一个跨命令累计的上限，让上层能「跑到额度就停、而不是撞墙才停」。
BUDGET_PATH = Path.home() / ".goofish-cli" / "run_budget.json"
DEFAULT_RUN_BUDGET = 0          # 0 = 不限（只计数）


def _read_budget() -> dict[str, Any]:
    if not BUDGET_PATH.exists():
        return {}
    try:
        raw = json.loads(BUDGET_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_budget(state: dict[str, Any]) -> None:
    BUDGET_PATH.parent.mkdir(parents=True, exist_ok=True)
    BUDGET_PATH.write_text(json.dumps(state, ensure_ascii=False))


def budget_used() -> int:
    """本轮已消耗的调用次数。"""
    try:
        return int(_read_budget().get("count", 0) or 0)
    except (TypeError, ValueError):
        return 0


def reset_budget() -> None:
    """开新一轮：清空计数。"""
    _write_budget({"count": 0, "started_at": time.time()})


def consume_budget(limit: int, *, label: str = "本轮") -> int:
    """消耗一次调用额度；超限抛 RateLimitedError。返回消耗后的已用次数。

    `limit <= 0` 表示不限（仍然计数，便于事后统计）。
    """
    state = _read_budget()
    try:
        used = int(state.get("count", 0) or 0)
    except (TypeError, ValueError):
        used = 0
    if limit > 0 and used >= limit:
        try:
            started = float(state.get("started_at", 0) or 0)
        except (TypeError, ValueError):
            started = 0.0
        began = time.strftime("%H:%M:%S", time.localtime(started)) if started else "?"
        raise RateLimitedError(
            f"{label}预算 {limit} 次调用已用尽（自 {began} 起计数）。"
            f"已抓到的数据已落盘；重跑会与已有快照合并，不需要重头再来。"
        )
    state["count"] = used + 1
    state.setdefault("started_at", time.time())
    state["last_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _write_budget(state)
    return used + 1
