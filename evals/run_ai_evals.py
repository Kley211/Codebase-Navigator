"""AI 报告评测：对评测集仓库生成 AI 概览，自动检查引用有效性与幻觉风险。

指标定义：
- precise_rate   引用可精确/唯一解析的比例（exact + basename + suffix 唯一命中）
- hallucination_rate  引用指向仓库中不存在文件的占比（幻觉风险，目标为 0）
- ambiguous     文件存在但同名多个 → 引用不精确，警告不计为幻觉
- missing_sections  必备章节覆盖

用法：
  python evals/run_ai_evals.py --filter flask,requests
  python evals/run_ai_evals.py --limit 2
  python evals/run_ai_evals.py --provider openrouter --model z-ai/glm-5.2:free
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.agent import CodebaseNavigator
from src.config import PROVIDERS, resolve_config
from src.repo import clone_repo
from src.tools.file_explorer import _walk_filtered

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 报告必须覆盖的关键章节
REQUIRED_SECTIONS = ["项目类型", "技术栈", "目录", "运行方式", "核心模块", "学习路线"]

# 引用格式：path.ext:line（行号可带范围如 3-6）
CITATION_RE = re.compile(
    r"([A-Za-z0-9_./-]+\.(?:py|md|toml|js|ts|tsx|jsx|go|rs|java|rb|yaml|yml|json|html|css|rst|txt|sh|sql|cfg|ini|lock))"
    r":(\d+)(?:-\d+)?"
)


def extract_citations(report: str) -> list[str]:
    """提取报告中所有 文件:行号 引用的文件路径（含重复）。"""
    return [m.group(1) for m in CITATION_RE.finditer(report)]


def _classify_citation(rel: str, repo_path: Path) -> tuple[str | None, str]:
    """三级解析引用，返回 (唯一解析路径或 None, 状态)。

    状态：
      exact / basename / suffix → 可验证（精确或唯一解析）
      ambiguous                 → 文件存在但同名多个（引用不精确，警告）
      not_found                 → 仓库中不存在（幻觉风险）
    """
    candidate = repo_path / rel.replace("/", os.sep)
    if candidate.exists():
        return rel, "exact"
    files = list(_walk_filtered(repo_path))
    by_name = [p for p in files if p.name == Path(rel).name]
    if len(by_name) == 1:
        return str(by_name[0]), "basename"
    tail = rel.replace("/", os.sep)
    by_suffix = [p for p in files if str(p).endswith(tail)]
    if len(by_suffix) == 1:
        return str(by_suffix[0]), "suffix"
    if by_name:
        return None, "ambiguous"
    return None, "not_found"


def evaluate_report(report: str, repo_path: Path) -> dict:
    cited = extract_citations(report)
    valid: list[str] = []
    ambiguous: list[str] = []
    not_found: list[str] = []
    status_counts = {"exact": 0, "basename": 0, "suffix": 0, "ambiguous": 0, "not_found": 0}

    for rel in dict.fromkeys(cited):  # 去重、保序
        resolved, status = _classify_citation(rel, repo_path)
        status_counts[status] += 1
        if status == "ambiguous":
            ambiguous.append(rel)
        elif status == "not_found":
            not_found.append(rel)
        elif resolved is not None:
            valid.append(resolved)

    missing_sections = [s for s in REQUIRED_SECTIONS if s not in report]
    total_unique = len(valid) + len(ambiguous) + len(not_found)
    precise_rate = round(len(valid) / total_unique, 3) if total_unique else 0.0
    hallucination_rate = round(len(not_found) / total_unique, 3) if total_unique else 0.0

    return {
        "chars": len(report),
        "citations_total": len(cited),
        "citations_unique": total_unique,
        "citations_valid": len(valid),
        "citations_precise_rate": precise_rate,
        "citations_hallucination_rate": hallucination_rate,
        "citation_status": status_counts,
        "missing_sections": missing_sections,
        "ambiguous_citations": ambiguous[:10],
        "not_found_citations": not_found[:10],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Codebase Navigator AI 报告评测")
    parser.add_argument("--clone-dir", default=str(Path(__file__).parent / ".cache"))
    parser.add_argument("--limit", type=int, help="只评测前 N 个仓库")
    parser.add_argument("--filter", help="按名字过滤（逗号分隔）")
    parser.add_argument("--provider", choices=list(PROVIDERS), default="openrouter")
    parser.add_argument("--model", help="模型名称")
    parser.add_argument("--save", help="保存报告原文到目录（便于人工核对幻觉）")
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

    try:
        config = resolve_config(provider=args.provider, model=args.model)
    except Exception as e:
        print(f"❌ {e}")
        return 1

    save_dir = Path(args.save) if args.save else None
    results = []
    failed = 0
    print(f"模型：{config.provider} / {config.model}\n")

    for item in repos:
        name = item["name"]
        dest = cache_dir / name
        print(f"▶ {name}：生成 AI 概览...", flush=True)
        if not (dest.exists() and any(dest.iterdir())):
            try:
                clone_repo(item["url"], dest=dest)
            except Exception as e:
                print(f"  ❌ 克隆失败：{e}")
                results.append({"name": name, "ok": False, "error": str(e)})
                failed += 1
                continue

        try:
            agent = CodebaseNavigator(str(dest), config)
            t0 = time.time()
            report = agent.get_overview()
            elapsed = time.time() - t0
        except Exception as e:
            print(f"  ❌ 生成失败：{e}")
            results.append({"name": name, "ok": False, "error": str(e)})
            failed += 1
            continue

        metrics = evaluate_report(report, dest)
        metrics["name"] = name
        metrics["elapsed"] = round(elapsed, 1)
        metrics["ok"] = (
            metrics["citations_hallucination_rate"] == 0
            and metrics["citations_precise_rate"] >= 0.85
            and not metrics["missing_sections"]
        )
        if not metrics["ok"]:
            failed += 1
        results.append(metrics)

        print(
            f"  {'✅' if metrics['ok'] else '❌'} 耗时{metrics['elapsed']}s "
            f"引用{metrics['citations_total']}条 精确率{metrics['citations_precise_rate']:.0%} "
            f"幻觉率{metrics['citations_hallucination_rate']:.0%} "
            f"缺章节{metrics['missing_sections'] or '无'}"
        )
        if metrics["ambiguous_citations"]:
            print(f"    ⚠ 歧义引用（同名文件）：{metrics['ambiguous_citations']}")
        if metrics["not_found_citations"]:
            print(f"    🚨 找不到的引用（幻觉风险）：{metrics['not_found_citations']}")

        if save_dir:
            save_dir.mkdir(parents=True, exist_ok=True)
            (save_dir / f"{name}_overview.md").write_text(report, encoding="utf-8")

    passed = len(results) - failed
    print(f"\n{'=' * 50}")
    print(f"AI 评测完成：{passed}/{len(results)} 通过")
    for r in results:
        if r.get("ok"):
            print(f"  ✅ {r['name']}  精确率{r['citations_precise_rate']:.0%} 幻觉率{r['citations_hallucination_rate']:.0%}")
        else:
            print(f"  ❌ {r['name']}  {r.get('error') or r.get('missing_sections')}")

    out_path = cache_dir / "ai_eval_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存：{out_path}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
