"""工具注册表：所有可被 LLM 调用的确定性工具。"""

from __future__ import annotations

from .file_explorer import (
    list_directory_structure,
    read_file,
    search_code,
    find_files_by_pattern,
)
from .code_analyzer import (
    get_imports,
    find_entry_points,
    analyze_dependencies,
    get_function_signatures,
)

TOOLS = [
    list_directory_structure,
    read_file,
    search_code,
    find_files_by_pattern,
    get_imports,
    find_entry_points,
    analyze_dependencies,
    get_function_signatures,
]

TOOL_MAP = {tool.__name__: tool for tool in TOOLS}

# OpenAI 兼容的 function calling 描述（供 LLM 使用）
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_directory_structure",
            "description": "列出仓库目录结构，过滤 node_modules、.git 等噪声目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "仓库根目录绝对路径"},
                    "max_depth": {"type": "integer", "description": "最大遍历深度，默认 4"},
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容（带行号），用于核实代码细节。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件的绝对路径"},
                    "max_lines": {"type": "integer", "description": "最多读取行数，默认 500"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_code",
            "description": "在代码库中按正则搜索代码，返回 文件:行号:内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "仓库根目录绝对路径"},
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "file_extension": {"type": "string", "description": "可选文件扩展名过滤，如 .py"},
                    "max_results": {"type": "integer", "description": "最多返回条数，默认 20"},
                },
                "required": ["repo_path", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files_by_pattern",
            "description": "按 glob 模式查找文件，例如 '**/*.py'。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "仓库根目录绝对路径"},
                    "pattern": {"type": "string", "description": "glob 模式"},
                    "max_results": {"type": "integer", "description": "最多返回条数，默认 30"},
                },
                "required": ["repo_path", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_imports",
            "description": "提取文件的 import 语句，按标准库/第三方/本地分类。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件的绝对路径"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_entry_points",
            "description": "识别代码库的主要入口点（main/app 文件、package.json scripts 等）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "仓库根目录绝对路径"},
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_dependencies",
            "description": "分析项目依赖（requirements.txt / pyproject.toml / package.json / go.mod / Cargo.toml）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string", "description": "仓库根目录绝对路径"},
                },
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_function_signatures",
            "description": "提取文件的函数/类签名（含行号），用于快速了解文件结构。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件的绝对路径"},
                },
                "required": ["file_path"],
            },
        },
    },
]


def get_tool_schemas() -> list[dict]:
    """返回所有工具 schema（OpenAI function calling 格式）。"""
    return TOOL_SCHEMAS


def call_tool(name: str, args: dict) -> str:
    """执行工具并返回字符串结果；任何异常都转为错误信息。"""
    tool = TOOL_MAP.get(name)
    if tool is None:
        return f"错误：未知工具 {name}"
    try:
        return str(tool(**args))
    except TypeError as e:
        return f"错误：工具参数不正确 - {e}"
    except Exception as e:
        return f"错误：工具执行失败 - {e}"