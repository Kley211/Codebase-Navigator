"""仓库加载：支持 GitHub URL / Gitee 镜像 / 本地路径。

URL 仓库浅克隆后会缓存到 ~/.codebase-navigator/repos（可用环境变量
CODEBASE_NAV_REPO_CACHE 覆盖），再次加载同一仓库直接复用缓存，避免反复联网
（GitHub 在国内网络不稳定）。想拉最新代码：删除 ~/.codebase-navigator/repos 下
对应的目录即可。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# URL 仓库的本地缓存根目录
REPO_CACHE_ROOT = Path(
    os.environ.get("CODEBASE_NAV_REPO_CACHE")
    or (Path.home() / ".codebase-navigator" / "repos")
)


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


def cache_dir_for(url: str) -> Path:
    """按 URL 计算稳定的缓存目录名（host__owner__repo）。"""
    url = normalize_url(url).rstrip("/")
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        owner, repo = m.group(1), m.group(2)
        host = re.search(r"://([^/]+)", url)
        host = host.group(1).split(":")[0].split(".")[0] if host else "git"
        return REPO_CACHE_ROOT / f"{host}__{owner}__{repo}"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", url)[-70:] or "repo"
    return REPO_CACHE_ROOT / f"{name}__{digest}"


def _is_usable_clone(path: Path) -> bool:
    return path.is_dir() and (path / ".git").exists() and any(path.iterdir())


def _friendly_clone_error(url: str, stderr: str) -> str:
    low = stderr.lower()
    if "not found" in low or "could not read" in low or "repository" in low and "access" in low:
        return (
            f"仓库不存在或无访问权限：{url}\n"
            "请检查拼写；私有仓库需先本地克隆，再用本地路径加载。"
        )
    if (
        "timed out" in low
        or "connection" in low
        or "unable to access" in low
        or "could not resolve host" in low
        or "reset by peer" in low
    ):
        return (
            "网络无法访问该仓库（GitHub 在国内网络常超时）：\n"
            f"{stderr}\n\n"
            "建议：① 稍后重试；② 换 Gitee 镜像或可访问的仓库地址；"
            "③ 在别处克隆好代码后，用本地路径加载（python cli.py <本地目录> --report）。"
        )
    return f"克隆失败：\n{stderr}"


def clone_repo(url: str, dest: Path | None = None, timeout: int = 300) -> Path:
    """浅克隆仓库；dest 为空时使用本地缓存（命中直接复用）。"""
    url = normalize_url(url)
    cached = dest is None
    dest = dest or cache_dir_for(url)
    if cached and _is_usable_clone(dest):
        return dest

    tmp = Path(tempfile.mkdtemp(prefix="codebase-nav-clone-"))
    try:
        result = subprocess.run(
            ["git", "clone", "--depth=1", url, str(tmp)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"克隆超时（{timeout} 秒），请尝试更小的仓库或使用本地路径。")
    if result.returncode != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(_friendly_clone_error(url, result.stderr.strip()))

    if not cached:
        return tmp
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    shutil.move(str(tmp), str(dest))
    return dest


def load_repo(target: str) -> Path:
    """加载仓库：URL / owner-repo 简写 → 克隆到本地缓存（可复用）；本地路径 → 直接使用。"""
    target = normalize_url(target)
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
