"""全局测试隔离。

为什么需要它：`mtop.call()` 入口现在会走 `guard.check()` 与 `limiter.wait_turn()`
（见 core/mtop.py）。若不隔离，任何间接调用 mtop 的测试都会：

  1. 写真实的 `~/.goofish-cli/limiter.json` / `circuit.json` —— 污染用户环境；
  2. 命中读限流的真实 sleep —— 测试套件从 9s 涨到 70s+。

所以这里把两个状态文件重定向到 tmp_path，并把读限流上限调得足够高，
让集成类测试不被节流拖慢。**专门验证限流行为的测试会自己 setenv 覆盖。**
"""
from __future__ import annotations

import pytest

_FAST_READ_RPM = "100000"


@pytest.fixture(autouse=True)
def _isolate_goofish_state(tmp_path, monkeypatch):
    state_dir = tmp_path / "goofish-cli-state"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "goofish_cli.core.limiter.STATE_PATH", state_dir / "limiter.json", raising=False)
    monkeypatch.setattr(
        "goofish_cli.core.guard.STATE_PATH", state_dir / "circuit.json", raising=False)
    monkeypatch.setattr(
        "goofish_cli.core.limiter.BUDGET_PATH", state_dir / "run_budget.json", raising=False)
    # 读限流默认放到很高、抖动关掉，避免测试被真实 sleep 拖慢；
    # 专门验证限流行为的测试会自己 setenv / 改 READ_JITTER 覆盖。
    monkeypatch.setenv("GOOFISH_READ_RPM", _FAST_READ_RPM)
    monkeypatch.setattr("goofish_cli.core.limiter.READ_JITTER", 0.0, raising=False)
    yield
