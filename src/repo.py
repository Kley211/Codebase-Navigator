"""仓库加载：支持 GitHub URL / Gitee 镜像 / 本地路径。"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def is_url(target: str) -> bool:
    return target.startswith(("http://", "https://", "git@", "ssh://"))


def normalize_url(url: str) -> str:
    """把 'owner/repo' 简写补全为 GitHub URL。"""
    url = url.strip()
    if url.startswith(("http://", "https://", "git@", "ssh://")):
        return url
    if re.match(r"^[\w.-]+/[\w.-]+$", url):
        return f"https://github.com/{url}"
    return url


def clone_repo(url: str, dest: Path | None = None, timeout: int = 300) -> Path:
    """浅克隆仓库到目标目录（默认临时目录），返回仓库路径。"""
    url = normalize_url(url)
    dest = dest or Path(tempfile.mkdtemp(prefix="codebase-nav-"))
    try:
        result = subprocess.run(
            ["git", "clone", "--depth=1", url, str(dest)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"克隆超时（{timeout} 秒），请尝试更小的仓库或使用本地路径。")
    if result.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(f"克隆失败：\n{result.stderr.strip()}")
    return dest


def load_repo(target: str) -> Path:
    """加载仓库：URL → 克隆到临时目录；本地路径 → 直接使用。"""
    if is_url(target):
        return clone_repo(target)
    path = Path(target).expanduser().resolve()
    if not path.exists():
        raise RuntimeError(f"路径不存在：{target}")
    if not path.is_dir():
        raise RuntimeError(f"不是目录：{target}")
    if not any(path.iterdir()):
        raise RuntimeError(f"目录为空：{target}")
    return path