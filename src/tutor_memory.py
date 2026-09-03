"""带读记忆（Learning Agent 闭环）：步骤级断点续读 + 薄弱点标记 + 学习者画像。

画像：把历史掌握度浓缩成一段文本，供「重新生成剧本」时注入提示词，
让新路线跳过已掌握内容、加深薄弱点（记忆驱动的自适应路线）。

数据文件默认位于 ~/.codebase-navigator/tutor_memory.json（与 progress.json 同目录）。
结构：{
  "<仓库名>": {
    "titles": ["第 1 步标题", ...],            # 用于校验剧本是否换过（换剧本则作废）
    "steps":  [{"passed": bool, "weak": bool}, ...],
    "updated_at": float
  }
}

为什么这样设计（可讲）：
- “进度”不是用户手勾的清单，而是 Agent 真实带读结果：完成一步 = 苏格拉底自检通过（+可选动手）。
- 薄弱点 = 该步某个自检要点被追问多次才覆盖，说明这里值得下次复述。
- titles 快照避免“重新生成剧本后继续旧进度”造成的错位。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

_DEFAULT_DIR = Path(os.path.expanduser("~")) / ".codebase-navigator"


def default_path() -> Path:
    return _DEFAULT_DIR / "tutor_memory.json"


def _backup_corrupt(p: Path) -> None:
    """损坏的记忆文件先备份再忽略，避免下次保存直接覆盖丢数据。"""
    try:
        if p.exists():
            bak = p.with_name(p.name + f".corrupt-{int(time.time())}")
            os.replace(p, bak)
    except OSError:
        pass


def load(path: Path | str | None = None) -> dict:
    p = Path(path) if path else default_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except OSError:
        return {}
    except (ValueError, TypeError):
        _backup_corrupt(p)
        return {}


def save(data: dict, path: Path | str | None = None) -> None:
    p = Path(path) if path else default_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass  # 记忆写失败不影响带读主流程


def ensure_entry(data: dict, repo_key: str, titles: list[str]) -> dict:
    """拿到某个仓库的记忆条目；剧本标题变了则重置（重新生成过剧本）。"""
    entry = data.get(repo_key)
    if not isinstance(entry, dict) or entry.get("titles") != list(titles):
        entry = {
            "titles": list(titles),
            "steps": [{"passed": False, "weak": False} for _ in titles],
            "updated_at": time.time(),
        }
        data[repo_key] = entry
    return entry


def entry_for(data: dict, repo_key: str, titles: list[str]) -> dict | None:
    """仅当剧本与记忆一致时返回条目（防止旧进度错位到新剧本）。"""
    entry = data.get(repo_key) if isinstance(data, dict) else None
    if isinstance(entry, dict) and entry.get("titles") == list(titles):
        return entry
    return None


def mark(entry: dict, idx: int, *, passed: bool | None = None, weak: bool | None = None) -> None:
    """写入某一步的结果：显式传 False 会清除旧标记（重学补强后移除 ⚠）。"""
    steps = entry.get("steps")
    if not isinstance(steps, list) or not (0 <= idx < len(steps)):
        return
    st = steps[idx]
    if passed is not None:
        st["passed"] = bool(passed)
    if weak is not None:
        st["weak"] = bool(weak)
    entry["updated_at"] = time.time()


def completed_count(entry: dict) -> int:
    return sum(1 for s in entry.get("steps", []) if s.get("passed"))


def weak_indexes(entry: dict) -> list[int]:
    return [i for i, s in enumerate(entry.get("steps", [])) if s.get("weak")]


def next_index(entry: dict) -> int:
    """下一个未完成步骤下标；全部完成则返回 len(steps)。"""
    steps = entry.get("steps", [])
    for i, s in enumerate(steps):
        if not s.get("passed"):
            return i
    return len(steps)


def profile(data: dict, repo_key: str, titles: list[str]) -> str | None:
    """把某仓库的带读记忆浓缩成「学习者画像」文本。

    用于重新生成剧本时让 LLM 调路线：默认跳过已掌握主题（最多快速回顾），
    把步骤预算留给薄弱点与尚未完成的内容。没有有效进度时返回 None。
    """
    entry = entry_for(data, repo_key, list(titles))
    if not entry:
        return None
    steps = entry.get("steps", [])
    passed_idx = [i for i, s in enumerate(steps) if s.get("passed")]
    weak_idx = [i for i, s in enumerate(steps) if s.get("weak")]
    if not passed_idx and not weak_idx:
        return None
    lines = [f"- 已完成 {len(passed_idx)}/{len(steps)} 步。"]
    if weak_idx:
        weak_names = "、".join(
            titles[i] for i in weak_idx if 0 <= i < len(titles)
        )
        lines.append(f"- 薄弱点（需加深）：{weak_names}。")
    remaining = [
        titles[i] for i in range(len(steps))
        if i not in passed_idx and 0 <= i < len(titles)
    ]
    if remaining:
        shown = "、".join(remaining[:5]) + ("…" if len(remaining) > 5 else "")
        lines.append(f"- 尚未完成：{shown}。")
    return "\n".join(lines)
