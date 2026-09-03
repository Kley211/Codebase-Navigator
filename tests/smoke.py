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
from src.context import build_overview_context, _is_large, _module_layout
from src.progress import MILESTONES, ProgressStore


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


def make_large_fake_repo() -> Path:
    """构造超过大仓库阈值（300 源码文件）的分层仓库。"""
    tmp = Path(tempfile.mkdtemp(prefix="nav-large-"))
    (tmp / "README.md").write_text("# Large Demo\n", encoding="utf-8")
    for module in ("core", "api", "worker", "cli"):
        for i in range(80):
            (tmp / module).mkdir(parents=True, exist_ok=True)
            (tmp / module / f"mod_{module}_{i:03d}.py").write_text(
                f"def fn{i}():\n    return {i}\n", encoding="utf-8"
            )
    return tmp


def main() -> int:
    repo = make_fake_repo()
    large_repo = make_large_fake_repo()
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

    if _is_large(repo):
        print("[❌] 小仓库不应被判定为大仓库")
        failed += 1
    if not _is_large(large_repo):
        print("[❌] 大仓库应被判定为大仓库")
        failed += 1

    modules = [name for name, _ in _module_layout(large_repo)]
    if "core" not in modules or "api" not in modules:
        print(f"[❌] 大仓库模块识别缺失：{modules}")
        failed += 1
    large_context = build_overview_context(str(large_repo))
    for expected in ("## 模块：core/", "## 模块：api/", "### 文件：core/"):
        if expected not in large_context:
            print(f"[❌] 大仓库分层上下文缺少：{expected}")
            failed += 1

    # 学习进度存储：初始化 → 勾选 → 持久化 → 重置
    progress_path = Path(tempfile.mkdtemp(prefix="nav-progress-")) / "progress.json"
    pstore = ProgressStore(progress_path)
    pstore.ensure("demo", str(repo))
    items = pstore.items("demo")
    for milestone in MILESTONES:
        if milestone not in items:
            print(f"[❌] 学习清单缺少里程碑：{milestone}")
            failed += 1
    if len(items) <= len(MILESTONES):
        print(f"[❌] 学习清单应包含关键文件项：{items}")
        failed += 1
    pstore.update("demo", items[:2])
    if ProgressStore(progress_path).done("demo") != items[:2]:
        print("[❌] 学习进度未持久化")
        failed += 1
    pstore.reset("demo")
    if ProgressStore(progress_path).done("demo"):
        print("[❌] 学习进度重置失败")
        failed += 1

    if failed == 0:
        print("[✅] 静态报告")
    else:
        print("[❌] 静态报告")
    print(f"\n结果：{len(checks) + 1 - failed}/{len(checks) + 1} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
