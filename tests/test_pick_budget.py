"""验单轮调用预算与撞风控等待续跑（两层护栏的行为）。"""
from __future__ import annotations

import pytest

from goofish_cli.commands.pick import collect as collect_cmd
from goofish_cli.core import limiter
from goofish_cli.core.errors import RateLimitedError


def test_budget_counts_up_to_limit():
    limiter.reset_budget()
    assert limiter.consume_budget(3) == 1
    assert limiter.consume_budget(3) == 2
    assert limiter.consume_budget(3) == 3
    assert limiter.budget_used() == 3


def test_budget_raises_when_exhausted():
    limiter.reset_budget()
    limiter.consume_budget(2)
    limiter.consume_budget(2)
    with pytest.raises(RateLimitedError) as excinfo:
        limiter.consume_budget(2)
    assert "预算" in str(excinfo.value)
    assert "已落盘" in str(excinfo.value), "报错要说明数据没丢，否则用户会以为白跑了"


def test_budget_zero_means_unlimited_but_still_counts():
    limiter.reset_budget()
    for _ in range(50):
        limiter.consume_budget(0)
    assert limiter.budget_used() == 50


def test_reset_budget_zeroes_count():
    limiter.consume_budget(10)
    limiter.reset_budget()
    assert limiter.budget_used() == 0


def test_budget_survives_across_gate_instances():
    """预算是跨命令累计的（写在文件里），不是进程内计数。"""
    limiter.reset_budget()
    limiter.consume_budget(5)
    assert limiter.budget_used() == 1        # 重新读文件也应看到计数


# ── 撞风控等待续跑 ──────────────────────────────────────────────────────

def test_wait_cooldown_disabled_returns_false(monkeypatch):
    monkeypatch.setattr(collect_cmd.guard, "remaining", lambda: 600.0)
    state = {"waits": 0}
    assert collect_cmd._wait_cooldown(False, state) is False
    assert state["waits"] == 0


def test_wait_cooldown_no_active_cooldown_returns_false(monkeypatch):
    monkeypatch.setattr(collect_cmd.guard, "remaining", lambda: 0.0)
    assert collect_cmd._wait_cooldown(True, {"waits": 0}) is False


def test_wait_cooldown_sleeps_until_expired(monkeypatch):
    """冷却期间要真的等到归零再返回，而不是睡一片就放行。"""
    marks = iter([900.0, 600.0, 0.0])

    def fake_remaining() -> float:
        return next(marks, 0.0)

    slept: list[float] = []
    monkeypatch.setattr(collect_cmd.guard, "remaining", fake_remaining)
    monkeypatch.setattr(collect_cmd.time, "sleep", lambda s: slept.append(s))

    state = {"waits": 0}
    assert collect_cmd._wait_cooldown(True, state) is True
    assert state["waits"] == 1
    assert len(slept) == 2
    assert all(s <= 30.0 for s in slept), "单片不得超过 30s（保持可中断）"


def test_wait_cooldown_gives_up_after_max_waits(monkeypatch):
    monkeypatch.setattr(collect_cmd.guard, "remaining", lambda: 600.0)
    state = {"waits": collect_cmd.MAX_COOLDOWN_WAITS}
    assert collect_cmd._wait_cooldown(True, state) is False
