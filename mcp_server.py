"""Codebase Navigator MCP Server。

把代码库分析能力暴露为 MCP 工具，供 Codex / Claude Desktop / Cursor 等 MCP 客户端调用。

启动（stdio）：
  python mcp_server.py

客户端配置示例（如 Codex 的 mcp config / claude_desktop_config.json）：
  {
    "mcpServers": {
      "codebase-navigator": {
        "command": "python",
        "args": ["E:/study/Codebase Navigator/mcp_server.py"]
      }
    }
  }
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from mcp.server.mcpserver import MCPServer

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from src.agent import CodebaseNavigator
from src.config import resolve_config
from src.report import generate_report
from src.repo import clone_repo
from src.tools.code_analyzer import (
    analyze_dependencies as _analyze_dependencies,
    find_entry_points as _find_entry_points,
    get_function_signatures as _get_function_signatures,
    get_imports as _get_imports,
)
from src.tools.file_explorer import (
    find_files_by_pattern as _find_files_by_pattern,
    list_directory_structure as _list_directory_structure,
    read_file as _read_file,
    search_code as _search_code,
)

server = MCPServer(
    "codebase-navigator",
    title="Codebase Navigator",
    description="帮助开发者快速理解陌生代码库：确定性静态分析 + AI 结构化学习概览（带 file:line 引用）",
    version="0.1.0",
)

# 克隆缓存目录（跨调用复用，避免重复克隆）
_CACHE_DIR = Path(tempfile.gettempdir()) / "codebase-nav-mcp"


@server.tool()
def load_repo(target: str) -> str:
    """加载代码库：GitHub URL 会浅克隆到本地缓存目录，本地路径直接使用；返回仓库根目录的绝对路径。"""
    if target.startswith(("http://", "https://", "git@", "ssh://")):
        name = target.rstrip("/").split("/")[-1] or "repo"
        dest = _CACHE_DIR / name
        if dest.exists() and any(dest.iterdir()):
            return str(dest)
        return str(clone_repo(target, dest=dest))
    path = Path(target).expanduser().resolve()
    if not path.is_dir():
        return f"错误：路径不存在 - {target}"
    return str(path)


@server.tool()
def list_directory_structure(repo_path: str, max_depth: int = 4) -> str:
    """列出仓库目录结构（过滤 node_modules、.git 等噪声目录），返回格式化树。"""
    return _list_directory_structure(repo_path, max_depth)


@server.tool()
def read_file(file_path: str, max_lines: int = 500) -> str:
    """读取文件内容（带行号，超限自动截断），用于核实代码细节。"""
    return _read_file(file_path, max_lines)


@server.tool()
def search_code(repo_path: str, pattern: str, file_extension: str | None = None, max_results: int = 20) -> str:
    """在代码库中按正则搜索代码，返回 文件:行号:内容。"""
    return _search_code(repo_path, pattern, file_extension, max_results)


@server.tool()
def find_files_by_pattern(repo_path: str, pattern: str, max_results: int = 30) -> str:
    """按 glob 模式查找文件，例如 '**/*.py'、'src/**/*test*'。"""
    return _find_files_by_pattern(repo_path, pattern, max_results)


@server.tool()
def get_imports(file_path: str) -> str:
    """提取文件的 import 语句，按 标准库/第三方/本地 分类。"""
    return _get_imports(file_path)


@server.tool()
def find_entry_points(repo_path: str) -> str:
    """识别代码库的主要入口点（main/app 文件、package.json scripts 等）。"""
    return _find_entry_points(repo_path)


@server.tool()
def analyze_dependencies(repo_path: str) -> str:
    """分析项目依赖（requirements.txt / pyproject.toml / package.json / go.mod / Cargo.toml）。"""
    return _analyze_dependencies(repo_path)


@server.tool()
def get_function_signatures(file_path: str) -> str:
    """提取文件中的函数/类签名（含行号）。"""
    return _get_function_signatures(file_path)


@server.tool()
def generate_static_report(repo_path: str) -> str:
    """生成静态学习报告（无需 API Key）：项目概览、目录结构、入口点、依赖、核心文件、学习路线。"""
    return generate_report(repo_path)


@server.tool()
def generate_ai_overview(repo_path: str, provider: str = "openrouter", model: str | None = None) -> str:
    """生成 AI 代码库学习概览（需要 API Key，默认读取 .env 的 OPENROUTER_API_KEY，免费模型 z-ai/glm-5.2:free）：带 file:line 引用的结构化报告。"""
    try:
        config = resolve_config(provider=provider, model=model)
        agent = CodebaseNavigator(repo_path, config)
        return agent.get_overview()
    except Exception as e:
        return f"错误：{e}"


if __name__ == "__main__":
    server.run()
