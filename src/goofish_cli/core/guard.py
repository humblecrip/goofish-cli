"""风控熔断。检测到 RiskControlError 后写入熔断时间戳，后续请求直接拒绝。

熔断时长默认 15 分钟：实测服务端 RGV587 冷却约 12-14 分钟（例如 15:35:5x 触发，
15:47 仍被拒，15:49:59 恢复），原来的 10 分钟会偏早放行、撞上仍在冷却的窗口。

`trip(reason)` 会把触发原因与接口一起落盘，`check()` 抛出时带上它 —— 否则
调用方（尤其 MCP agent）只知道"失败了"，无法判断该等多久、也不该重试。
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from goofish_cli.core.errors import RiskControlError

STATE_PATH = Path.home() / ".goofish-cli" / "circuit.json"
DEFAULT_BREAK_MINUTES = 15
_REASON_MAX = 200


def _break_seconds() -> int:
    try:
        return max(60, int(os.environ.get("GOOFISH_CIRCUIT_BREAK_MINUTES", DEFAULT_BREAK_MINUTES)) * 60)
    except ValueError:
        return DEFAULT_BREAK_MINUTES * 60


def _load_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    try:
        raw = json.loads(STATE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _load() -> float:
    """熔断到期时间戳（0 表示未熔断）。"""
    try:
        return float(_load_state().get("until", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _save(until: float, reason: str = "") -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state: dict[str, object] = {"until": until}
    if until:
        state["tripped_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if reason:
            state["reason"] = reason[:_REASON_MAX]
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False))


def check() -> None:
    state = _load_state()
    try:
        until = float(state.get("until", 0) or 0)
    except (TypeError, ValueError):
        return
    if until and time.time() < until:
        remain = int(until - time.time())
        reason = str(state.get("reason") or "").strip()
        hint = f"（触发：{reason}）" if reason else ""
        raise RiskControlError(
            f"风控熔断中，剩余 {remain}s{hint}。触发后自动冷却，"
            f"可通过 `goofish auth reset-guard` 手动解除。"
        )


def trip(reason: str = "") -> None:
    _save(time.time() + _break_seconds(), reason)


def reset() -> None:
    if STATE_PATH.exists():
        STATE_PATH.unlink()


@contextmanager
def watch():
    """包住写操作：命中 RGV587 自动熔断。

    读操作不需要包这个 —— `mtop.call` 入口已经统一做了 `check()` 与
    `trip(reason)`（见 core/mtop.py）。
    """
    check()
    try:
        yield
    except RiskControlError:
        trip()
        raise
