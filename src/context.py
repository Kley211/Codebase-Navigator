"""静态上下文构建：把仓库静态分析结果拼成供 LLM 单次生成报告的上下文。

背景：多轮 ReAct 在部分大仓库（Go/Rust/TS）下，模型会把工具调用以文本形式输出，
导致循环卡死、最终回答质量差。改为「静态分析 → 一次 LLM 调用」：
- 上下文 = 目录结构 + 依赖 + 入口点 + README 摘要 + 核心文件内容（带真实行号）
- 模型无需自主探索，直接基于真实内容生成报告
- 预算控制：固定块限流，核心文件始终保留足够份额，避免大 README 挤占

大仓库分层（源码文件数 >= LARGE_REPO_MIN_FILES）：
- 全局层：目录结构（深度 2）+ 依赖 + 入口点 + README 摘要
- 模块层：按源码规模取 Top 顶层模块，每个模块给出「模块树 + 模块内核心文件」
- 保证每个主要模块都有带行号的真实内容可引用，避免 Top-N 大文件全落在单一模块
"""

from __future__ import annotations

import re
from pathlib import Path

from .report import _readme_digest
from .tools.code_analyzer import ENTRY_POINT_PATTERNS, analyze_dependencies, find_entry_points
from .tools.file_explorer import IGNORE_DIRS, _walk_filtered, list_directory_structure

MAX_CONTEXT_CHARS = 30_000  # 上下文总字符预算
MAX_FILE_LINES = 120        # 单个核心文件最多展示行数（普通仓库）
MAX_CORE_FILES = 8          # 最多纳入的核心文件数（普通仓库）
MAX_ENTRY_FILES = 3         # 入口文件最多占用的名额
EST_CHARS_PER_LINE = 45     # 用于自适应估算每行平均字符数

LARGE_REPO_MIN_FILES = 300  # 源码文件数超过该值视为大仓库，启用分层
MAX_MODULES = 6             # 分层模式下最多展示的顶层模块数
MODULE_BUDGET = 2600        # 每个模块的上下文预算（字符）

# 固定块各自的预算上限（字符）
_BUDGETS = {"tree": 4000, "deps": 2000, "entry": 1500, "readme": 3000}

# 非学习重点目录（fixtures/examples/playground/docs 等），不参与核心文件与模块选择
_NOISE_DIRS = {
    "docs", "doc", "test", "tests", "testing", "examples", "example",
    "fixtures", "fixture", "playground", "benchmarks", "bench",
    "vendor", "node_modules", "target", "dist", "build", ".github",
    "scripts", "tools", "assets", "resources", "res", "flow-typed", "misc",
}

# 测试文件特征（不纳入核心文件内容，避免挤占预算）
_TEST_RE = re.compile(r"(^|[/\\])(__tests__/|tests?/|test_|.*[._](test|spec)\.)", re.I)

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


def _is_noise(rel: Path) -> bool:
    """相对路径是否位于噪声目录中（fixtures/examples/docs 等）。"""
    return any(part in _NOISE_DIRS for part in rel.parts)


def _count_source_files(repo: Path) -> int:
    """统计源码文件数（用于大仓库判定）。"""
    return sum(1 for rel in _walk_filtered(repo) if rel.suffix.lower() in SOURCE_EXTENSIONS)


def _is_large(repo: Path) -> bool:
    """是否为大仓库（源码文件数达到阈值即判定）。"""
    count = 0
    for rel in _walk_filtered(repo):
        if rel.suffix.lower() in SOURCE_EXTENSIONS:
            count += 1
            if count >= LARGE_REPO_MIN_FILES:
                return True
    return False


def _entry_rels(repo: Path) -> list[str]:
    """找出常见入口文件名（如 main.py / index.ts），返回相对路径列表。"""
    rels = []
    for patterns in ENTRY_POINT_PATTERNS.values():
        for pattern in patterns:
            for match in repo.glob(f"**/{pattern}"):
                rel = match.relative_to(repo)
                text = str(rel).replace("\\", "/")
                if any(part in IGNORE_DIRS for part in rel.parts) or _is_noise(rel) or _TEST_RE.search(text):
                    continue
                rels.append(str(rel).replace("\\", "/"))
    return rels


def _top_source_files(repo: Path, base: Path, limit: int) -> list[str]:
    """返回 base 目录下按文件大小排序的 Top 源码文件（排除测试与噪声）。"""
    ranked: list[tuple[int, str]] = []
    for rel in _walk_filtered(base):
        if rel.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        text = str(rel).replace("\\", "/")
        if _is_noise(rel) or _TEST_RE.search(text):
            continue
        try:
            size = (base / rel).stat().st_size
        except OSError:
            continue
        ranked.append((size, text))
    ranked.sort(key=lambda x: x[0], reverse=True)
    return [rel for _, rel in ranked[:limit]]


def _pick_core_files(repo: Path, max_files: int, entry_rels: list[str]) -> list[str]:
    """选择核心文件（普通仓库）：入口文件优先，其余按文件大小从大到小（排除测试/噪声）。"""
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
            if text in picked or _is_noise(rel) or _TEST_RE.search(text):
                continue
            try:
                size = (repo / rel).stat().st_size
            except OSError:
                continue
            ranked.append((size, text))
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


def _module_size(path: Path) -> tuple[bool, int]:
    """返回 (是否含源码, 源码总字节数)。"""
    size = 0
    has_source = False
    for rel in _walk_filtered(path):
        if rel.suffix.lower() in SOURCE_EXTENSIONS:
            has_source = True
            try:
                size += (path / rel).stat().st_size
            except OSError:
                pass
    return has_source, size


def _root_source_size(path: Path) -> int:
    """返回目录根层级（不含子目录）源码文件的总字节数。"""
    size = 0
    for child in path.iterdir():
        if child.is_file() and child.suffix.lower() in SOURCE_EXTENSIONS:
            try:
                size += child.stat().st_size
            except OSError:
                pass
    return size


def _module_layout(repo: Path) -> list[tuple[str, int]]:
    """识别源码模块，返回 (模块相对路径, 源码字节数) 按规模降序。

    顶层目录若含 4 个以上带源码的子目录（如 monorepo 的 packages/crates），
    则展开为子模块，保证模块粒度适中。
    """
    modules: list[tuple[str, int]] = []
    for entry in sorted(repo.iterdir()):
        if not entry.is_dir() or entry.name in _NOISE_DIRS:
            continue
        has_source, size = _module_size(entry)
        if not has_source:
            continue
        subs = []
        for sub in sorted(entry.iterdir()):
            if sub.is_dir() and sub.name not in _NOISE_DIRS:
                has_sub_source, sub_size = _module_size(sub)
                if has_sub_source:
                    subs.append((sub.name, sub_size))
        if len(subs) >= 3:
            root_size = _root_source_size(entry)
            if root_size > 0:
                modules.append((entry.name, root_size))
            for name, sub_size in subs:
                modules.append((f"{entry.name}/{name}", sub_size))
        else:
            modules.append((entry.name, size))
    modules.sort(key=lambda x: x[1], reverse=True)
    return modules


def _build_large_context(repo: Path) -> str:
    """大仓库分层上下文：全局层 + 模块层。"""
    header = f"# 仓库：{repo.name}"
    blocks = [
        "## 目录结构（顶层）\n```text\n" + _clip(list_directory_structure(str(repo), max_depth=2), _BUDGETS["tree"]) + "\n```",
        "## 依赖\n```text\n" + _clip(analyze_dependencies(str(repo)), _BUDGETS["deps"]) + "\n```",
        "## 入口点\n```text\n" + _clip(find_entry_points(str(repo)), _BUDGETS["entry"]) + "\n```",
        "## README 摘要\n" + _clip(_readme_digest(repo), _BUDGETS["readme"]),
    ]

    # 全局核心文件：仅保留入口文件（数量少、有真实行号），供运行方式/入口章节引用
    entry_blocks = []
    for rel in _entry_rels(repo)[:MAX_ENTRY_FILES]:
        block = _file_block(rel, repo, 80)
        if len(block) > MODULE_BUDGET:
            break
        entry_blocks.append(block)
    if entry_blocks:
        blocks.append("## 核心入口文件（含真实行号）\n" + "\n\n".join(entry_blocks))

    # 模块层：每个模块 = 模块树（深度 2）+ 模块内 Top 源码文件
    for name, size in _module_layout(repo)[:MAX_MODULES]:
        module_path = repo / name
        sub_tree = _clip(list_directory_structure(str(module_path), max_depth=2), MODULE_BUDGET // 3)
        remaining = MODULE_BUDGET - len(sub_tree)
        per_file_lines = min(80, max(25, remaining // (2 * EST_CHARS_PER_LINE)))
        file_blocks = []
        for rel in _top_source_files(module_path, module_path, 2):
            block = _file_block(f"{name}/{rel}", repo, per_file_lines)
            if len(block) > remaining:
                break
            file_blocks.append(block)
            remaining -= len(block)
        module_block = [f"## 模块：{name}/（源码约 {size // 1024} KB）", "```text", sub_tree, "```"]
        module_block += file_blocks
        blocks.append("\n".join(module_block))

    return "\n\n".join(blocks)


def build_overview_context(repo_path: str) -> str:
    """构建供单次生成使用的仓库静态上下文。"""
    repo = Path(repo_path).resolve()
    if not repo.exists():
        return f"错误：仓库路径不存在 - {repo_path}"

    if _is_large(repo):
        return _build_large_context(repo)

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
