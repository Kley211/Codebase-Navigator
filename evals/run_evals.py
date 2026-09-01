"""评测脚本：对评测集仓库运行静态分析 + 报告生成，统计通过率。

用法：
  python evals/run_evals.py                      # 克隆（有缓存则复用）并评测全部仓库
  python evals/run_evals.py --filter flask,gin   # 只跑指定仓库
  python evals/run_evals.py --limit 3            # 只跑前 3 个
  python evals/run_evals.py --local              # 只用缓存，不克隆
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.repo import clone_repo
from src.report import generate_report
from src.tools import call_tool

# 每个仓库必须通过的确定性工具检查
TOOL_CHECKS = [
    ("list_directory_structure", {"max_depth": 3}),
    ("find_entry_points", {}),
    ("analyze_dependencies", {}),
    ("search_code", {"pattern": "TODO|FIXME"}),
]


def run_repo(repo_path: Path) -> dict:
    results: dict = {"tools": {}, "report_ok": False, "errors": []}

    for name, extra in TOOL_CHECKS:
        args = {"repo_path": str(repo_path), **extra}
        out = call_tool(name, args)
        ok = not out.startswith("错误")
        results["tools"][name] = "ok" if ok else "error"
        if not ok:
            results["errors"].append(f"{name}: {out}")

    try:
        report = generate_report(str(repo_path))
        results["report_ok"] = "项目学习报告" in report
    except Exception as e:
        results["errors"].append(f"report: {e}")

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Codebase Navigator 评测")
    parser.add_argument("--clone-dir", default=str(Path(__file__).parent / ".cache"))
    parser.add_argument("--local", action="store_true", help="使用已有缓存，不克隆")
    parser.add_argument("--limit", type=int, help="只评测前 N 个仓库")
    parser.add_argument("--filter", help="按名字过滤（逗号分隔，如 flask,gin）")
    args = parser.parse_args()

    cache_dir = Path(args.clone_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    with open(Path(__file__).parent / "repos.yaml", encoding="utf-8") as f:
        repos = yaml.safe_load(f)["repos"]

    if args.filter:
        wanted = {n.strip() for n in args.filter.split(",") if n.strip()}
        repos = [r for r in repos if r["name"] in wanted]
    if args.limit:
        repos = repos[: args.limit]

    summary = []
    failed = 0
    for item in repos:
        name = item["name"]
        dest = cache_dir / name
        print(f"\n▶ {name}（{item['language']} / {item['size']}）...", flush=True)
        if dest.exists() and any(dest.iterdir()):
            print("  使用已有缓存。")
        else:
            try:
                clone_repo(item["url"], dest=dest)
            except Exception as e:
                print(f"  ❌ 克隆失败：{e}")
                summary.append({"name": name, "ok": False, "error": str(e)})
                failed += 1
                continue

        t0 = time.time()
        res = run_repo(dest)
        elapsed = time.time() - t0
        ok = res["report_ok"] and all(v == "ok" for v in res["tools"].values())
        if not ok:
            failed += 1
        res["name"] = name
        res["ok"] = ok
        res["elapsed"] = round(elapsed, 1)
        summary.append(res)

        tool_str = ", ".join(f"{k}={v}" for k, v in res["tools"].items())
        print(f"  {'✅' if ok else '❌'} 工具[{tool_str}] 报告={'ok' if res['report_ok'] else 'fail'} 耗时 {res['elapsed']}s")
        for err in res["errors"]:
            print(f"    ⚠ {err}")

    passed = len(summary) - failed
    print(f"\n{'=' * 50}")
    print(f"评测完成：{passed}/{len(summary)} 通过")
    for item in summary:
        status = "✅" if item["ok"] else "❌"
        print(f"  {status} {item['name']}  {item.get('elapsed', '-')}s")

    out_path = cache_dir / "eval_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存：{out_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())