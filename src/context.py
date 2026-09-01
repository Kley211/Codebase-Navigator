"""静态上下文构建：把仓库静态分析结果拼成供 LLM 单次生成报告的上下文。

背景：多轮 ReAct 在部分大仓库（Go/Rust/TS）下，模型会把工具调用以文本形式输出，
导致循环卡死、最终回答质量差。改为「静态分析 → 一次 LLM 调用」：
- 上下文 = 目录结构 + 依赖 + 入口点 + README 摘要 + 核心文件内容（带真实行号）
- 模型无需自主探索，直接基于真实内容生成报告
- 预算控制：固定块限流，核心文件始终保留足够份额，避免大 README 挤占
"""

from __future__ import annotations

import re
from pathlib import Path

from .report import _readme_digest
from .tools.code_analyzer import ENTRY_POINT_PATTERNS, analyze_dependencies, find_entry_points
from .tools.file_explorer import IGNORE_DIRS, _walk_filtered, list_directory_structure

MAX_CONTEXT_CHARS = 30_000  # 上下文总字符预算
MAX_FILE_LINES = 120        # 单个核心文件最多展示行数
MAX_CORE_FILES = 8          # 最多纳入的核心文件数
MAX_ENTRY_FILES = 3         # 入口文件最多占用的名额
EST_CHARS_PER_LINE = 45     # 用于自适应估算每行平均字符数

# 固定块各自的预算上限（字符）
_BUDGETS = {"tree": 4000, "deps": 2000, "entry": 1500, "readme": 3000}

# 测试文件特征（不纳入核心文件内容，避免挤占预算）
_TEST_RE = re.compile(r"(^|[/\\])(__tests__/|tests?/|test_|.*\.(test|spec)\.)", re.I)

# 真正的源码扩展名（排除 json/md/toml 等数据文件）
SOURCE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb",
    ".php", ".cs", ".cpp", ".c", ".h", ".hpp", ".swift", ".kt", ".scala",
    ".vue", ".svelte", ".sh",
}


def _clip(text: str, limit: int) -> str:
    """按字符上限截断，尽量在行尾断开。"""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    idx = cut.rfind("\n")
    if idx > limit // 2:
        cut = cut[:idx]
    return cut + "\n…（内容超预算截断）"


def _entry_rels(repo: Path) -> list[str]:
    """找出常见入口文件名（如 main.py / index.ts），返回相对路径列表。"""
    rels = []
    for patterns in ENTRY_POINT_PATTERNS.values():
        for pattern in patterns:
            for match in repo.glob(f"**/{pattern}"):
                rel = match.relative_to(repo)
                if any(part in IGNORE_DIRS for part in rel.parts):
                    continue
                rels.append(str(rel).replace("\\", "/"))
    return rels


def _pick_core_files(repo: Path, max_files: int, entry_rels: list[str]) -> list[str]:
    """选择核心文件：入口文件优先，其余按代码行数从大到小（排除测试）。"""
    picked = [
        rel for rel in entry_rels
        if (repo / rel).is_file() and not _TEST_RE.search(rel)
    ][:MAX_ENTRY_FILES]
    remaining = max_files - len(picked)
    if remaining > 0:
        ranked: list[tuple[int, str]] = []
        for rel in _walk_filtered(repo):
            if rel.suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            text = str(rel).replace("\\", "/")
            if text in picked or _TEST_RE.search(text):
                continue
            try:
                loc = sum(1 for _ in (repo / rel).open("r", encoding="utf-8", errors="replace"))
            except OSError:
                continue
            ranked.append((loc, text))
        ranked.sort(key=lambda x: x[0], reverse=True)
        picked += [rel for _, rel in ranked[:remaining]]
    return picked


def _file_block(rel: str, repo: Path, max_lines: int) -> str:
    """读取文件前 max_lines 行并编号，返回带完整相对路径的代码块。"""
    path = repo / rel
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        return f"### 文件：{rel}\n（读取失败：{e}）"
    total = len(lines)
    shown = lines[:max_lines]
    numbered = [f"{i:4d} | {line}" for i, line in enumerate(shown, 1)]
    parts = [f"### 文件：{rel}（共 {total} 行，显示前 {len(shown)} 行）"]
    parts += numbered
    if total > max_lines:
        parts.append(f"…（剩余 {total - max_lines} 行省略）")
    return "\n".join(parts)


def build_overview_context(repo_path: str) -> str:
    """构建供单次生成使用的仓库静态上下文。"""
    repo = Path(repo_path).resolve()
    if not repo.exists():
        return f"错误：仓库路径不存在 - {repo_path}"

    header = f"# 仓库：{repo.name}"
    blocks = [
        "## 目录结构\n```text\n" + _clip(list_directory_structure(str(repo), max_depth=3), _BUDGETS["tree"]) + "\n```",
        "## 依赖\n```text\n" + _clip(analyze_dependencies(str(repo)), _BUDGETS["deps"]) + "\n```",
        "## 入口点\n```text\n" + _clip(find_entry_points(str(repo)), _BUDGETS["entry"]) + "\n```",
        "## README 摘要\n" + _clip(_readme_digest(repo), _BUDGETS["readme"]),
    ]
    fixed_used = len(header) + sum(len(b) for b in blocks)
    remaining = MAX_CONTEXT_CHARS - fixed_used

    # 核心文件按剩余预算自适应：保证每个文件有足够行数用于引用
    per_file_lines = min(
        MAX_FILE_LINES,
        max(40, remaining // (MAX_CORE_FILES * EST_CHARS_PER_LINE)),
    )
    entry_rels = _entry_rels(repo)
    file_blocks: list[str] = []
    for rel in _pick_core_files(repo, MAX_CORE_FILES, entry_rels):
        block = _file_block(rel, repo, per_file_lines)
        if len(block) > remaining:
            break
        file_blocks.append(block)
        remaining -= len(block)
    if file_blocks:
        blocks.append("## 核心文件（含真实行号，引用行号必须取自这里）\n" + "\n\n".join(file_blocks))

    return "\n\n".join([header, *blocks])
