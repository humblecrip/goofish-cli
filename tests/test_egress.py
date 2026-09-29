"""验出口 IP 预检：判定标准是「只有一族可达」，不是「每个 IP 都不变」。"""
from __future__ import annotations

from goofish_cli.core import egress


def test_only_one_family_is_stable():
    report = egress.analyze({"v4": ["1.2.3.4", "1.2.3.4"], "v6": []})
    assert report["stable"] is True
    assert report["reachable"] == ["v4"]
    assert report["problems"] == []


def test_both_families_is_unstable():
    """实测形态：v4/v6 各自稳定但两族都通 → 会话会从两个地址出去。"""
    report = egress.analyze({"v4": ["1.2.3.4"], "v6": ["2406:da18::1"]})
    assert report["stable"] is False
    assert any("双栈双出口" in p for p in report["problems"])
    assert report["reachable"] == ["v4", "v6"]


def test_rotating_exit_within_family_is_unstable():
    report = egress.analyze({"v4": ["1.1.1.1", "2.2.2.2"], "v6": []})
    assert report["stable"] is False
    assert any("轮换" in p for p in report["problems"])


def test_no_samples_is_unstable_not_silently_ok():
    """采样失败不能被当成"稳定"——那是把未知当成了安全。"""
    report = egress.analyze({"v4": [], "v6": []})
    assert report["stable"] is False
    assert any("采样失败" in p for p in report["problems"])


def test_sample_uses_family_specific_endpoints(monkeypatch):
    seen: list[str] = []

    class _Resp:
        def __init__(self, text: str) -> None:
            self.text = text

    def fake_get(url: str, timeout: float = 0) -> _Resp:
        seen.append(url)
        return _Resp("2406:da18::1" if "ipv6" in url or "api6" in url else "1.2.3.4")

    monkeypatch.setattr(egress.requests, "get", fake_get)
    out = egress.sample(samples=1)

    assert out["v4"] == ["1.2.3.4"]
    assert out["v6"] == ["2406:da18::1"]
    assert any("ipv4" in u for u in seen)
    assert any("ipv6" in u for u in seen)


def test_sample_skips_empty_and_erroring_endpoints(monkeypatch):
    class _Resp:
        def __init__(self, text: str) -> None:
            self.text = text

    def fake_get(url: str, timeout: float = 0) -> _Resp:
        if "ipv4" in url:
            return _Resp("")                       # 空响应 → 试下一个端点
        if "ipify" in url:
            raise egress.requests.RequestException("boom")
        return _Resp("9.9.9.9")

    monkeypatch.setattr(egress.requests, "get", fake_get)
    out = egress.sample(samples=1)
    assert out["v4"] == ["9.9.9.9"]
