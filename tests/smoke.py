"""冒烟测试：创建一个小仓库，验证工具与静态报告可用。

运行：python tests/smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.report import generate_report
from src.tools import call_tool
from src.context import build_overview_context


def make_fake_repo() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="nav-test-"))
    (tmp / "README.md").write_text(
        "# Demo\n\n一个用于测试的演示项目。\n\n## 安装\n\npip install -r requirements.txt\n",
        encoding="utf-8",
    )
    (tmp / "requirements.txt").write_text("requests>=2.0\nflask==3.0\n", encoding="utf-8")
    (tmp / "app.py").write_text(
        "import os\nfrom flask import Flask\n\napp = Flask(__name__)\n\n"
        "def hello():\n    return 'hi'\n\n"
        "if __name__ == '__main__':\n    app.run()\n",
        encoding="utf-8",
    )
    (tmp / "src").mkdir()
    (tmp / "src" / "core.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_app.py").write_text("def test_hello():\n    assert True\n", encoding="utf-8")
    return tmp


def main() -> int:
    repo = make_fake_repo()
    checks = [
        ("目录结构", call_tool("list_directory_structure", {"repo_path": str(repo)})),
        ("入口点", call_tool("find_entry_points", {"repo_path": str(repo)})),
        ("依赖", call_tool("analyze_dependencies", {"repo_path": str(repo)})),
        ("读文件", call_tool("read_file", {"file_path": str(repo / "app.py")})),
        ("搜索", call_tool("search_code", {"repo_path": str(repo), "pattern": "Flask"})),
        ("找文件", call_tool("find_files_by_pattern", {"repo_path": str(repo), "pattern": "**/*.py"})),
        ("imports", call_tool("get_imports", {"file_path": str(repo / "app.py")})),
        ("签名", call_tool("get_function_signatures", {"file_path": str(repo / "app.py")})),
    ]

    failed = 0
    for name, out in checks:
        ok = out and not out.startswith("错误")
        print(f"[{'✅' if ok else '❌'}] {name}")
        if not ok:
            failed += 1
            print(out)

    report = generate_report(str(repo))
    for expected in ("项目学习报告", "app.py", "flask", "学习路线"):
        if expected not in report:
            print(f"[❌] 报告缺少关键内容：{expected}")
            failed += 1

    context = build_overview_context(str(repo))
    for expected in ("目录结构", "依赖", "入口点", "### 文件：app.py", "src/core.py"):
        if expected not in context:
            print(f"[❌] AI 上下文缺少关键内容：{expected}")
            failed += 1

    if failed == 0:
        print("[✅] 静态报告")
    else:
        print("[❌] 静态报告")
    print(f"\n结果：{len(checks) + 1 - failed}/{len(checks) + 1} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
