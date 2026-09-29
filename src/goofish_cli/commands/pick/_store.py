"""pick 命名空间的落盘约定与数据读写。

默认全部落在 `~/.goofish-cli/picks/`：这是**全局命令**，不该往某个项目目录写数据。
所有路径都接受 `root` 参数与 `--out` 覆盖，测试因此可以完全隔离。

    <root>/
    ├── weights.json          用户权重（首次运行自动从包内模板复制）
    ├── keywords.txt          受管关键词表（pick collect 的默认输入）
    ├── snapshots/*.jsonl     快照（每轮一个文件）
    ├── vocabulary/*.csv      候选词频表
    └── reports/<ts>.{json,csv}   冻结的历史榜单（含本次使用的权重）
"""
from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

ROOT = Path.home() / ".goofish-cli" / "picks"

SUBDIRS = ("snapshots", "vocabulary", "reports")


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def parse_ts(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))


def ensure_dirs(root: Path | None = None) -> Path:
    base = root or ROOT
    base.mkdir(parents=True, exist_ok=True)
    for name in SUBDIRS:
        (base / name).mkdir(parents=True, exist_ok=True)
    return base


def _dir(root: Path | None, name: str) -> Path:
    return (root or ROOT) / name


# ── 权重 ────────────────────────────────────────────────────────────────

def default_weights() -> dict[str, Any]:
    """包内模板（随 wheel 发布，见 static/weights.default.json）。"""
    raw = files("goofish_cli.static").joinpath("weights.default.json").read_text(encoding="utf-8")
    return json.loads(raw)


def user_weights_path(root: Path | None = None) -> Path:
    return (root or ROOT) / "weights.json"


def load_weights(root: Path | None = None, override: str | None = None) -> tuple[dict[str, Any], str]:
    """返回 (权重配置, 来源说明)。

    优先级：`override`（JSON 串或文件路径）> 用户 weights.json > 包内模板。
    用户文件不存在时自动从模板复制一份 —— 保证「首次运行就有一份可改的文件」，
    否则容易出现「以为改了但代码里还是默认值」。
    """
    if override:
        text = override.strip()
        if text.startswith("{"):
            return json.loads(text), "命令行 --weights(JSON)"
        path = Path(text).expanduser()
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8")), f"--weights:{path}"
        raise ValueError(f"--weights 既不是 JSON 串也不是可读文件：{text[:60]}")

    path = user_weights_path(root)
    if not path.exists():
        ensure_dirs(root)
        template = default_weights()
        path.write_text(json.dumps(template, ensure_ascii=False, indent=2), encoding="utf-8")
        return template, f"模板（已复制到 {path}）"
    return json.loads(path.read_text(encoding="utf-8")), str(path)


# ── 关键词表 ────────────────────────────────────────────────────────────

def keywords_path(root: Path | None = None) -> Path:
    return (root or ROOT) / "keywords.txt"


def load_keywords(root: Path | None = None, path: str | None = None) -> list[str]:
    target = Path(path).expanduser() if path else keywords_path(root)
    if not target.is_file():
        raise FileNotFoundError(
            f"关键词表不存在：{target}。"
            f"先跑 `goofish pick vocab` 生成候选词并整理，或手动创建该文件（一行一词，# 开头为注释）。"
        )
    out = [ln.strip() for ln in target.read_text(encoding="utf-8").splitlines()
           if ln.strip() and not ln.strip().startswith("#")]
    if not out:
        raise ValueError(f"关键词表里没有有效词：{target}")
    return out


# ── 快照读写 ────────────────────────────────────────────────────────────

def write_snapshot(records: list[dict[str, Any]], root: Path | None = None) -> Path:
    ensure_dirs(root)
    path = _dir(root, "snapshots") / f"{stamp()}.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def iter_snapshots(root: Path | None = None) -> list[Path]:
    d = _dir(root, "snapshots")
    return sorted(d.glob("*.jsonl")) if d.exists() else []


def latest_snapshot(root: Path | None = None) -> Path | None:
    snaps = iter_snapshots(root)
    return snaps[-1] if snaps else None


def load_observations(root: Path | None = None) -> dict[str, Any]:
    """读全部快照，重建按实体的时间序列。

    返回 {"snapshots": [(ts, path)], "item": {id: [(ts, rec)]},
          "seller": {key: [(ts, rec)]}, "keyword": {kw: [(ts, rec)]}, "runs": [rec]}
    """
    obs: dict[str, Any] = {
        "snapshots": [],
        "item": {},
        "seller": {},
        "keyword": {},
        "runs": [],
    }
    for path in iter_snapshots(root):
        ts: datetime | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ts is None and rec.get("ts"):
                ts = parse_ts(rec["ts"])
            kind = rec.get("type")
            if kind == "item":
                obs["item"].setdefault(str(rec.get("item_id", "")), []).append((ts, rec))
            elif kind == "seller":
                key = rec.get("uid") or f"nick:{rec.get('nick')}"
                obs["seller"].setdefault(key, []).append((ts, rec))
            elif kind == "keyword":
                obs["keyword"].setdefault(rec.get("keyword", ""), []).append((ts, rec))
            elif kind == "run":
                obs["runs"].append(rec)
        if ts is not None:
            obs["snapshots"].append((ts, path))

    obs["snapshots"].sort(key=lambda pair: pair[0])
    for kind in ("item", "seller", "keyword"):
        for key in obs[kind]:
            obs[kind][key].sort(key=lambda pair: (pair[0] is None, pair[0]))
    return obs


def latest(series: list[tuple[datetime | None, dict[str, Any]]]) -> dict[str, Any]:
    return series[-1][1]


def rate_per_day(series: list[tuple[datetime | None, dict[str, Any]]], field: str,
                 min_days: float = 0.02) -> float | None:
    """Δfield / 天数。需要 ≥2 个有效观测，且间隔够长（否则除数趋零）。"""
    pts = [(t, r.get(field)) for t, r in series
           if t is not None and isinstance(r.get(field), (int, float))]
    if len(pts) < 2:
        return None
    days = (pts[-1][0] - pts[0][0]).total_seconds() / 86400
    if days < min_days:
        return None
    delta = pts[-1][1] - pts[0][1]
    if delta < 0:                      # 评价数不该下降；下降说明覆盖变了，不当速率用
        return None
    return delta / days


def snapshot_age_hours(root: Path | None = None) -> float | None:
    latest_snap = latest_snapshot(root)
    if latest_snap is None:
        return None
    mtime = datetime.fromtimestamp(latest_snap.stat().st_mtime, tz=UTC).astimezone()
    return (datetime.now(UTC).astimezone() - mtime).total_seconds() / 3600


# ── 报告落盘 ────────────────────────────────────────────────────────────

def write_json(data: dict[str, Any], root: Path | None = None, name: str | None = None) -> Path:
    ensure_dirs(root)
    path = _dir(root, "reports") / f"{name or stamp()}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def write_csv(rows: list[dict[str, Any]], columns: list[str], root: Path | None = None,
              name: str | None = None) -> Path:
    ensure_dirs(root)
    path = _dir(root, "reports") / f"{name or stamp()}.csv"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    path.write_text(buf.getvalue(), encoding="utf-8")
    return path


def write_vocab_csv(rows: list[dict[str, Any]], columns: list[str],
                    root: Path | None = None) -> Path:
    ensure_dirs(root)
    path = _dir(root, "vocabulary") / f"{stamp()}-candidates.csv"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: row.get(c, "") for c in columns})
    path.write_text(buf.getvalue(), encoding="utf-8")
    return path
