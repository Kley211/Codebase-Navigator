"""文件探索工具：纯确定性的文件系统操作，不依赖任何 AI。"""

from __future__ import annotations

import os
import re
from pathlib import Path

# 始终忽略的目录
IGNORE_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "coverage", ".pytest_cache",
    ".mypy_cache", ".tox", ".idea", ".vscode", "vendor", "target",
    "out", "bin", "obj", "site-packages", ".cache", ".gradle",
}

# 代码/文本文件扩展名（用于高亮、统计、搜索）
CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb",
    ".php", ".cs", ".cpp", ".c", ".h", ".hpp", ".swift", ".kt", ".scala",
    ".vue", ".svelte", ".sh", ".sql", ".html", ".css", ".scss", ".toml",
    ".yaml", ".yml", ".json", ".md",
}

# 重要配置文件
CONFIG_FILES = {
    "package.json", "requirements.txt", "pyproject.toml", "setup.py",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "Gemfile",
    "composer.json", "Makefile", "Dockerfile", "docker-compose.yml",
    "docker-compose.yaml", ".env.example", "tsconfig.json",
    "vite.config.ts", "next.config.js", "webpack.config.js", ".gitignore",
}


def should_ignore(path: Path) -> bool:
    """判断仓库内相对路径是否应被忽略。"""
    for part in path.parts:
        if part in IGNORE_DIRS:
            return True
        # 隐藏文件/目录（保留少数有用文件）
        if part.startswith(".") and part not in {".github", ".env.example", ".gitignore"}:
            return True
    return False


def _walk_filtered(repo: Path):
    """迭代仓库内所有未被忽略的文件，yield 相对路径。"""
    for dirpath, dirnames, filenames in os.walk(repo):
        dirpath_obj = Path(dirpath)
        rel_dir = dirpath_obj.relative_to(repo)
        dirnames[:] = [d for d in dirnames if not should_ignore(rel_dir / d)]
        for name in filenames:
            rel = rel_dir / name
            if not should_ignore(rel):
                yield rel


def list_directory_structure(repo_path: str, max_depth: int = 4) -> str:
    """列出仓库目录结构（过滤噪声），返回格式化树。"""
    repo = Path(repo_path)
    if not repo.exists():
        return f"错误：路径不存在 - {repo_path}"

    lines = [f"📁 {repo.name}/"]

    def walk(current: Path, prefix: str = "", depth: int = 0):
        if depth >= max_depth:
            lines.append(f"{prefix}└── ...（已达最大深度 {max_depth}）")
            return
        try:
            entries = sorted(current.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        except PermissionError:
            return
        entries = [e for e in entries if not should_ignore(e.relative_to(repo))]
        for i, entry in enumerate(entries):
            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            if entry.is_dir():
                lines.append(f"{prefix}{connector}📁 {entry.name}/")
                walk(entry, prefix + ("    " if is_last else "│   "), depth + 1)
            else:
                icon = "📄"
                if entry.name in CONFIG_FILES:
                    icon = "⚙️"
                elif entry.suffix in CODE_EXTENSIONS:
                    icon = "📝"
                elif entry.name == "README.md":
                    icon = "📖"
                lines.append(f"{prefix}{connector}{icon} {entry.name}")

    walk(repo)
    return "\n".join(lines)


def read_file(file_path: str, max_lines: int = 500) -> str:
    """读取文件内容，带行号输出（超限自动截断）。"""
    path = Path(file_path)
    if not path.exists():
        return f"错误：文件不存在 - {file_path}"
    if not path.is_file():
        return f"错误：不是文件 - {file_path}"
    size = path.stat().st_size
    if size > 1_000_000:
        return f"错误：文件过大（{size:,} 字节），请先定位具体区域再读取。"

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return f"读取失败：{e}"

    total = len(lines)
    shown = lines[:max_lines]
    numbered = [f"{i:4d} | {line.rstrip()}" for i, line in enumerate(shown, 1)]
    result = [f"📄 {path.name}（共 {total} 行）", "─" * 50, *numbered]
    if total > max_lines:
        result.append(f"\n... 已截断（还有 {total - max_lines} 行）")
    return "\n".join(result)


def search_code(
    repo_path: str,
    pattern: str,
    file_extension: str | None = None,
    max_results: int = 20,
) -> str:
    """在代码库中搜索正则表达式，返回 文件:行号:内容。"""
    repo = Path(repo_path)
    if not repo.exists():
        return f"错误：路径不存在 - {repo_path}"

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"错误：正则表达式无效 - {e}"

    matches = []
    for rel in _walk_filtered(repo):
        if file_extension and not str(rel).endswith(file_extension):
            continue
        try:
            with open(repo / rel, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if regex.search(line):
                        matches.append(f"{rel}:{lineno}: {line.rstrip()[:200]}")
                        if len(matches) >= max_results:
                            break
        except OSError:
            continue
        if len(matches) >= max_results:
            break

    if not matches:
        return f"未找到匹配：'{pattern}'"
    result = [f"🔍 搜索 '{pattern}' 的结果：", "─" * 50, *matches]
    if len(matches) >= max_results:
        result.append(f"\n... 仅显示前 {max_results} 条")
    return "\n".join(result)


def find_files_by_pattern(repo_path: str, pattern: str, max_results: int = 30) -> str:
    """按 glob 模式查找文件，例如 '**/*.py'、'src/**/*test*'。"""
    repo = Path(repo_path)
    if not repo.exists():
        return f"错误：路径不存在 - {repo_path}"

    matches = []
    for path in repo.glob(pattern):
        if not path.is_file():
            continue
        rel = path.relative_to(repo)
        if should_ignore(rel):
            continue
        matches.append(str(rel))
        if len(matches) >= max_results:
            break

    if not matches:
        return f"未找到匹配 '{pattern}' 的文件"
    result = [f"📁 匹配 '{pattern}' 的文件：", "─" * 50, *matches]
    if len(matches) >= max_results:
        result.append(f"\n... 仅显示前 {max_results} 条")
    return "\n".join(result)