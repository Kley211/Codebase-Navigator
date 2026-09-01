"""静态学习报告：不依赖 LLM，直接基于文件系统分析生成 Markdown 报告。"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from .tools.file_explorer import CODE_EXTENSIONS, _walk_filtered, list_directory_structure
from .tools.code_analyzer import analyze_dependencies, find_entry_points

# 扩展名 → 语言名
LANG_NAMES = {
    ".py": "Python", ".ts": "TypeScript", ".tsx": "TypeScript(React)",
    ".js": "JavaScript", ".jsx": "JavaScript(React)", ".go": "Go",
    ".rs": "Rust", ".java": "Java", ".rb": "Ruby", ".php": "PHP",
    ".cs": "C#", ".cpp": "C++", ".c": "C", ".h": "C/C++ 头文件",
    ".swift": "Swift", ".kt": "Kotlin", ".scala": "Scala", ".vue": "Vue",
    ".svelte": "Svelte", ".sh": "Shell", ".sql": "SQL", ".html": "HTML",
    ".css": "CSS", ".scss": "SCSS", ".toml": "TOML", ".yaml": "YAML",
    ".yml": "YAML", ".json": "JSON", ".md": "Markdown",
}


def _file_stats(repo: Path):
    """统计扩展名与代码行数，返回 Top 文件列表。"""
    ext_count: Counter = Counter()
    lang_loc: Counter = Counter()
    files_by_loc: list[tuple[str, int]] = []
    total_files = 0

    for rel in _walk_filtered(repo):
        total_files += 1
        ext = rel.suffix.lower()
        if ext in CODE_EXTENSIONS:
            ext_count[ext] += 1
            try:
                loc = sum(1 for _ in (repo / rel).open("r", encoding="utf-8", errors="replace"))
            except OSError:
                loc = 0
            lang_loc[ext] += loc
            files_by_loc.append((str(rel), loc))

    files_by_loc.sort(key=lambda x: x[1], reverse=True)
    return ext_count, lang_loc, total_files, files_by_loc


def _readme_digest(repo: Path) -> str:
    for name in ("README.md", "readme.md", "Readme.md", "README.rst", "README"):
        readme = repo / name
        if readme.is_file():
            text = readme.read_text(encoding="utf-8", errors="replace")
            headings = re.findall(r"^#{1,4}\s+.*$", text, re.M)
            excerpt_lines = text.splitlines()[:60]
            part = [f"📖 {name}"]
            if headings:
                part += ["", "**章节目录：**"]
                part += [f"- {h.lstrip('#').strip()}" for h in headings[:20]]
            part += ["", "**开头内容（前 60 行）：**", "", "```text", *excerpt_lines, "```"]
            return "\n".join(part)
    return "未找到 README 文件。"


def _learning_path(repo: Path) -> str:
    steps = ["1. **通读 README**：先了解项目定位、安装与运行方式。"]
    has_tests = any((repo / d).exists() for d in ("tests", "test"))
    if has_tests:
        steps.append("2. **跑通测试**：从测试用例反推功能行为，最快建立全局认知。")
    else:
        steps.append("2. **找入口点**：从 main/app 文件开始，沿调用链读下去。")
    steps.append("3. **抓核心模块**：优先阅读被反复 import 的核心文件。")
    steps.append("4. **看依赖**：结合依赖列表判断技术栈，再去官方文档补基础。")
    steps.append("5. **动手改**：跑起来后改一行代码看效果，再回头读实现。")
    return "\n".join(steps)


def generate_report(repo_path: str) -> str:
    """生成 Markdown 格式的静态学习报告（无 LLM）。"""
    repo = Path(repo_path).resolve()
    if not repo.exists():
        return f"错误：路径不存在 - {repo_path}"

    ext_count, lang_loc, total_files, core_files = _file_stats(repo)
    total_loc = sum(lang_loc.values())

    lang_lines = []
    for ext, loc in lang_loc.most_common():
        name = LANG_NAMES.get(ext, ext.lstrip(".").upper() or "?")
        lang_lines.append(f"- **{name}**：{loc:,} 行（{ext_count[ext]} 个文件）")

    tree = list_directory_structure(str(repo), max_depth=3)
    entry_text = find_entry_points(str(repo))
    dep_text = analyze_dependencies(str(repo))

    core_rows = [f"| `{rel}` | {loc:,} |" for rel, loc in core_files[:15]]
    core_table = "\n".join(["| 文件 | 行数 |", "|---|---|", *core_rows]) if core_rows else "（无代码文件）"

    readme = _readme_digest(repo)
    path_suggest = _learning_path(repo)

    return f"""# 📚 项目学习报告：{repo.name}

> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} · 无 LLM，纯静态分析

## 一、项目概览

- 总文件数：{total_files}
- 代码总量：约 {total_loc:,} 行

**语言分布：**
{chr(10).join(lang_lines) if lang_lines else "（未识别到代码文件）"}

## 二、目录结构

```text
{tree}
```

## 三、入口点

```text
{entry_text}
```

## 四、依赖

```text
{dep_text}
```

## 五、核心文件（按代码行数 Top 15）

{core_table}

> 提示：行数多 ≠ 最重要。真正的核心模块通常是被 import 次数最多的文件，AI 概览会进一步分析。

## 六、README 摘要

{readme}

## 七、学习路线建议

{path_suggest}
"""