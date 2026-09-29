"""pick preflight — 出口 IP 预检。

实测本机代理是**双栈双出口**：v4 与 v6 出口各自稳定，但两族都可达意味着同一会话的
请求会随连接走哪个协议族而从两个不同地址出去——这是已确认的风控触发因子之一。

所以判定"稳定"的标准不是"每个 IP 都不变"，而是**只有一族可达**。修复方向在代理层
（关掉一个协议族，或固定单一出口），不在本命令。
"""
from __future__ import annotations

from typing import Any

from goofish_cli.core import Strategy, command, egress
from goofish_cli.core.errors import GoofishError

COLUMNS = ["family", "reachable", "unique_ips", "ips", "verdict"]


@command(
    namespace="pick",
    name="preflight",
    description="出口 IP 预检（按 v4/v6 分别采样，检测双栈双出口）",
    strategy=Strategy.PUBLIC,
    columns=COLUMNS,
)
def preflight(samples: int = egress.DEFAULT_SAMPLES, strict: bool = False) -> dict[str, Any]:
    families = egress.sample(samples=samples)
    report = egress.analyze(families)

    rows: list[dict[str, Any]] = []
    for name in ("v4", "v6"):
        ips = report[name]
        rows.append({
            "family": name,
            "reachable": bool(ips),
            "unique_ips": len(ips),
            "ips": " | ".join(ips),
            "verdict": "可达" if ips else "不可达",
        })

    if strict and not report["stable"]:
        raise GoofishError(
            "拒绝启动：出口 IP 不稳定或无法确认（--strict）。"
            + "；".join(report["problems"])
        )

    return {"items": rows, **report}
