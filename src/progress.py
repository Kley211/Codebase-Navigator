"""学习进度：每个仓库的学习清单与勾选状态，保存为本地 JSON（无数据库）。

进度文件默认位于 ~/.codebase-navigator/progress.json，按仓库名隔离。
格式：{ "<仓库名>": {"items": [...], "done": [...], "updated_at": float} }
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

# 默认进度文件位置
_DEFAULT_DIR = Path(os.path.expanduser("~")) / ".codebase-navigator"

# 通用学习里程碑（每个仓库都会包含）
MILESTONES = [
    "README 与项目定位",
    "运行方式：安装 & 启动",
    "目录结构与架构理解",
    "核心模块职责梳理",
    "关键文件精读",
    "动手实验：改代码 / 写示例",
]


def default_items(repo_path: str) -> list[str]:
    """仓库默认学习清单：固定里程碑 + 按代码量 Top 3 的关键文件。"""
    items = list(MILESTONES)
    try:
        from .report import _file_stats

        repo = Path(repo_path)
        if repo.is_dir():
            _, _, _, files = _file_stats(repo)
            seen = set()
            for rel, _ in files:
                label = f"关键文件精读：{rel}"
                if label in seen:
                    continue
                seen.add(label)
                items.append(label)
                if len(items) >= len(MILESTONES) + 3:
                    break
    except Exception:
        pass
    return items


class ProgressStore:
    """读写本地进度 JSON。"""

    def __init__(self, path: Path | None = None):
        self.path = path or (_DEFAULT_DIR / "progress.json")

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except OSError:
            return {}
        except (ValueError, json.JSONDecodeError):
            self._backup_corrupt()
            return {}

    def _backup_corrupt(self) -> None:
        """进度文件损坏时先备份再忽略，避免下次保存直接覆盖导致数据丢失。"""
        try:
            if self.path.exists():
                bak = self.path.with_name(self.path.name + f".corrupt-{int(time.time())}")
                os.replace(self.path, bak)
        except OSError:
            pass

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    def get(self, repo_key: str) -> dict:
        """读取某仓库进度；不存在时返回空记录。"""
        return self._load().get(repo_key, {})

    def items(self, repo_key: str) -> list[str]:
        return self.get(repo_key).get("items", [])

    def done(self, repo_key: str) -> list[str]:
        return self.get(repo_key).get("done", [])

    def ensure(self, repo_key: str, repo_path: str) -> None:
        """仓库无进度记录时，用默认清单初始化。"""
        data = self._load()
        if repo_key in data and data[repo_key].get("items"):
            return
        data[repo_key] = {
            "items": default_items(repo_path),
            "done": [],
            "updated_at": time.time(),
        }
        self._save(data)

    def update(self, repo_key: str, done: list[str]) -> None:
        """更新勾选状态（保留已存清单）。"""
        data = self._load()
        record = data.get(repo_key)
        if not record:
            return
        record["done"] = list(done)
        record["updated_at"] = time.time()
        self._save(data)

    def reset(self, repo_key: str) -> None:
        """重置某仓库进度。"""
        data = self._load()
        if repo_key in data:
            data[repo_key]["done"] = []
            data[repo_key]["updated_at"] = time.time()
            self._save(data)
