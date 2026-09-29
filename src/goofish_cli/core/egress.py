"""出口 IP 采样与稳定性判定。

为什么需要：实测本机代理是**双栈双出口**——v4 出口与 v6 出口各自稳定，但两族
都可达，意味着同一会话的请求会随连接走哪个协议族而从**两个不同地址**出去。
这是已确认的风控触发因子之一（另一个是请求密度）。

用已依赖的 `requests` 而非 shell 调 curl：少一个外部可执行依赖，超时/异常
处理也更可控。
"""
from __future__ import annotations

from typing import Any

import requests

# v4-only / v6-only 端点。不能用"通用"端点——那样会掩盖地址族双出口，
# 而那正是要检测的东西。
V4_ENDPOINTS: tuple[str, ...] = (
    "https://ipv4.icanhazip.com",
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
)
V6_ENDPOINTS: tuple[str, ...] = (
    "https://ipv6.icanhazip.com",
    "https://api6.ipify.org",
)
DEFAULT_SAMPLES = 3
DEFAULT_TIMEOUT = 8.0


def _probe(endpoints: tuple[str, ...], samples: int, timeout: float) -> list[str]:
    """逐端点尝试，取到非空结果即算一次成功采样。"""
    out: list[str] = []
    for _ in range(max(1, samples)):
        for url in endpoints:
            try:
                resp = requests.get(url, timeout=timeout)
                text = (resp.text or "").strip()
            except requests.RequestException:
                continue
            if text and " " not in text and len(text) <= 64:
                out.append(text)
                break
    return out


def sample(samples: int = DEFAULT_SAMPLES, timeout: float = DEFAULT_TIMEOUT) -> dict[str, list[str]]:
    """按协议族分别采样出口 IP。返回 {"v4": [...], "v6": [...]}。"""
    return {
        "v4": _probe(V4_ENDPOINTS, samples, timeout),
        "v6": _probe(V6_ENDPOINTS, samples, timeout),
    }


def analyze(families: dict[str, list[str]]) -> dict[str, Any]:
    """判定出口稳定性。

    "稳定"的唯一标准：**只有一族可达**。两族都通意味着会话地址不唯一。
    """
    v4 = sorted(set(families.get("v4") or []))
    v6 = sorted(set(families.get("v6") or []))
    reachable = [name for name, ips in (("v4", v4), ("v6", v6)) if ips]

    problems: list[str] = []
    if not reachable:
        problems.append("采样失败：v4/v6 端点都没有返回（检查代理与网络）")
    if len(reachable) > 1:
        problems.append("双栈双出口：v4 与 v6 都可达 → 会话可能从 2 个地址出去")
    for name, ips in (("v4", v4), ("v6", v6)):
        if len(ips) > 1:
            problems.append(f"{name} 出口轮换：{len(ips)} 个不同 IP")

    return {
        "v4": v4,
        "v6": v6,
        "reachable": reachable,
        "problems": problems,
        "stable": not problems,
    }
