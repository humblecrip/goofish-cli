"""验读路径安全：入口熔断、读限流、触发原因落盘。

对应三处改动：
  1. `guard.py` 熔断 10 → 15 分钟（实测服务端冷却 12-14 分钟）+ 触发原因落盘
  2. `limiter.py` 新增读路径**等待式**限流（写路径仍是拒绝式，语义不同）
  3. `mtop.call()` 入口统一 `guard.check()` + `limiter.wait_turn()`，
     命中风控时 `guard.trip(reason)` —— 原版这些只覆盖 3 个写命令
"""
from __future__ import annotations

import time

import pytest

from goofish_cli.core.errors import RiskControlError

# ── guard：熔断时长与原因 ────────────────────────────────────────────────

def test_default_break_is_15_minutes():
    """10 分钟会偏早放行（实测冷却 12-14 分钟）。"""
    from goofish_cli.core import guard

    assert guard.DEFAULT_BREAK_MINUTES == 15
    assert guard._break_seconds() == 900


def test_trip_records_reason_and_check_reports_it(tmp_path, monkeypatch):
    monkeypatch.setattr("goofish_cli.core.guard.STATE_PATH", tmp_path / "circuit.json")
    monkeypatch.setenv("GOOFISH_CIRCUIT_BREAK_MINUTES", "60")
    from goofish_cli.core import guard

    guard.trip("mtop.taobao.idle.pc.detail: FAIL_SYS_USER_VALIDATE | RGV587_ERROR")

    with pytest.raises(RiskControlError) as excinfo:
        guard.check()
    msg = str(excinfo.value)
    assert "熔断中" in msg
    assert "idle.pc.detail" in msg, "触发接口应出现在提示里，便于诊断"
    assert "RGV587" in msg


def test_load_still_returns_timestamp(tmp_path, monkeypatch):
    """向后兼容：`_load()` 仍返回 float 时间戳（既有测试与调用方依赖）。"""
    monkeypatch.setattr("goofish_cli.core.guard.STATE_PATH", tmp_path / "circuit.json")
    from goofish_cli.core import guard

    assert guard._load() == 0
    guard.trip("x")
    assert isinstance(guard._load(), float)
    assert guard._load() > time.time()


def test_reset_also_clears_reason(tmp_path, monkeypatch):
    monkeypatch.setattr("goofish_cli.core.guard.STATE_PATH", tmp_path / "circuit.json")
    from goofish_cli.core import guard

    guard.trip("RGV587")
    guard.reset()
    assert guard._load() == 0
    guard.check()  # 不抛
    with guard.watch():
        pass


def test_check_tolerates_corrupt_state(tmp_path, monkeypatch):
    path = tmp_path / "circuit.json"
    monkeypatch.setattr("goofish_cli.core.guard.STATE_PATH", path)
    path.write_text("{ 不是 json")
    from goofish_cli.core import guard

    guard.check()  # 不抛
    assert guard._load() == 0


# ── limiter：读路径限流规则 ──────────────────────────────────────────────

def test_read_bucket_limits(monkeypatch):
    monkeypatch.setenv("GOOFISH_READ_RPM", "120")
    from goofish_cli.core.limiter import read_bucket

    # detail 实测 4-6 次即触发风控，所以给最紧的额度
    assert read_bucket("mtop.taobao.idle.pc.detail")[1] == 3
    assert read_bucket("mtop.taobao.idlemtopsearch.pc.search")[1] == 15
    assert read_bucket("mtop.idle.web.trade.rate.list")[1] == 6
    assert read_bucket("mtop.whatever.else")[1] == 120


def test_read_rpm_zero_disables(monkeypatch):
    monkeypatch.setenv("GOOFISH_READ_RPM", "0")
    from goofish_cli.core.limiter import read_bucket, wait_turn

    assert read_bucket("mtop.taobao.idle.pc.detail")[1] == 0
    wait_turn("mtop.taobao.idle.pc.detail")  # 不 sleep、不写状态


def test_read_rpm_caps_per_api_limit(monkeypatch):
    """全局上限调小后，应压过接口的独立上限。"""
    monkeypatch.setenv("GOOFISH_READ_RPM", "2")
    from goofish_cli.core.limiter import read_bucket

    assert read_bucket("mtop.taobao.idle.pc.detail")[1] == 2
    assert read_bucket("mtop.other")[1] == 2


class _Clock:
    """假时钟：sleep 推进时间。避免测试真的等 60s 窗口。"""

    def __init__(self) -> None:
        self.t = 1_000_000.0
        self.slept: list[float] = []

    def time(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        # 只记录真正会阻塞的等待（尾部有 0 时长的抖动 sleep，不应算作"等待"）
        if seconds > 0:
            self.slept.append(seconds)
        self.t += seconds


def _use_clock(monkeypatch, limiter) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(limiter.time, "time", clock.time)
    monkeypatch.setattr(limiter.time, "sleep", clock.sleep)
    return clock


def test_wait_turn_waits_when_window_full(tmp_path, monkeypatch):
    """等待式：窗口满时 sleep 等额度，而不是抛 RateLimitedError。"""
    monkeypatch.setattr("goofish_cli.core.limiter.STATE_PATH", tmp_path / "limiter.json")
    monkeypatch.setenv("GOOFISH_READ_RPM", "1")
    from goofish_cli.core import limiter

    clock = _use_clock(monkeypatch, limiter)
    monkeypatch.setattr(limiter, "READ_JITTER", 0.0)

    limiter.wait_turn("mtop.some.read")          # 第 1 次：有额度，不等待
    assert clock.slept == []

    limiter.wait_turn("mtop.some.read")          # 第 2 次：分片等到窗口真的腾出额度
    assert sum(clock.slept) >= 59, f"累计等待应覆盖整个窗口，实际 {clock.slept}"
    assert len(clock.slept) >= 2, "应分片等待（单片上限 30s），而不是一次睡满"
    assert all(s <= 31 for s in clock.slept), "单片不应超过 30s 上限"


def test_wait_turn_buckets_are_independent(tmp_path, monkeypatch):
    monkeypatch.setattr("goofish_cli.core.limiter.STATE_PATH", tmp_path / "limiter.json")
    monkeypatch.setenv("GOOFISH_READ_RPM", "1")
    from goofish_cli.core import limiter

    clock = _use_clock(monkeypatch, limiter)
    monkeypatch.setattr(limiter, "READ_JITTER", 0.0)

    limiter.wait_turn("mtop.taobao.idle.pc.detail")
    limiter.wait_turn("mtop.taobao.idlemtopsearch.pc.search")   # 不同 bucket，不该等
    assert clock.slept == []


# ── mtop.call：入口守卫 ──────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _Http:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def post(self, *args: object, **kwargs: object) -> _Resp:
        self.calls += 1
        return _Resp(self.payload)


class _Session:
    """只实现 mtop.call 需要的两个接口。"""

    def __init__(self, payload: dict) -> None:
        self.http = _Http(payload)

    @property
    def h5_token(self) -> str:
        return "deadbeef_token"


def _stub_sign(monkeypatch):
    """跳过 execjs/Node 依赖。"""
    from goofish_cli.core import mtop

    monkeypatch.setattr(mtop, "generate_sign", lambda t, token, data: "sig")


def test_call_is_blocked_by_circuit(monkeypatch):
    """熔断生效时，**读**命令也必须在发 HTTP 之前被拒（原版读路径不受约束）。"""
    from goofish_cli.core import mtop

    _stub_sign(monkeypatch)
    monkeypatch.setattr(mtop.guard, "check", lambda: (_ for _ in ()).throw(
        RiskControlError("风控熔断中，剩余 900s")))

    session = _Session({"ret": ["SUCCESS::调用成功"]})
    with pytest.raises(RiskControlError):
        mtop.call(session, api="mtop.taobao.idle.pc.detail", data={"itemId": "1"})
    assert session.http.calls == 0, "被熔断时不应发出请求"


def test_call_trips_guard_with_api_name(monkeypatch):
    """命中风控时把触发接口写进熔断原因。"""
    from goofish_cli.core import mtop

    _stub_sign(monkeypatch)
    monkeypatch.setattr(mtop.guard, "check", lambda: None)
    tripped: list[str] = []
    monkeypatch.setattr(mtop.guard, "trip", lambda reason="": tripped.append(reason))

    session = _Session({"ret": ["FAIL_SYS_USER_VALIDATE::哎哟喂,被挤爆啦"]})
    with pytest.raises(RiskControlError):
        mtop.call(session, api="mtop.taobao.idle.pc.detail", data={"itemId": "1"})

    assert tripped, "应调用 guard.trip 落盘熔断"
    assert "mtop.taobao.idle.pc.detail" in tripped[0]


def test_call_throttles_read(monkeypatch):
    """入口应调用读限流（等待式），并可被关闭。"""
    from goofish_cli.core import mtop

    _stub_sign(monkeypatch)
    monkeypatch.setattr(mtop.guard, "check", lambda: None)
    seen: list[str] = []
    monkeypatch.setattr(mtop.limiter, "wait_turn", lambda api: seen.append(api))

    session = _Session({"ret": ["SUCCESS::调用成功"]})
    mtop.call(session, api="mtop.taobao.idlemtopsearch.pc.search", data={"pageNumber": 1})

    assert seen == ["mtop.taobao.idlemtopsearch.pc.search"]
    assert session.http.calls == 1
