"""MCP Server 冒烟验证：stdio 连接 → 列工具 → 调用工具。"""
import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(__file__).resolve().parent.parent / "mcp_server.py")],
        env=None,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("tools:", names)

            res = await session.call_tool(
                "list_directory_structure",
                {"repo_path": str(Path(__file__).resolve().parent.parent / "tests")},
            )
            text = "".join(c.text for c in res.content)
            print("--- list_directory_structure(tests) ---")
            print(text[:300])

            res2 = await session.call_tool(
                "analyze_dependencies",
                {"repo_path": str(Path(__file__).resolve().parent.parent)},
            )
            text2 = "".join(c.text for c in res2.content)
            print("--- analyze_dependencies(root) ---")
            print(text2[:300])

            bad = await session.call_tool("read_file", {"file_path": "Z:/not_exist.py"})
            print("--- read_file(missing) ---")
            print("".join(c.text for c in bad.content)[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
