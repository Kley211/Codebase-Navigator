"""Agent 行为评测：Ask 问答路径的任务成功率与工具效率（不是报告质量评测）。

与 run_ai_evals.py 的区别：这里评测的是「Agent 行为」——
- 任务成功率：回答是否引用到金标准文件，且无幻觉引用（指向不存在文件）
- 金标文件真实触达率：Agent 是否真的 read/import 过目标文件（而不是靠模型猜）
- 工具效率：每个任务平均工具调用数与耗时
- 幻觉率：回答引用中指向仓库不存在文件的占比

用法：
  python evals/run_agent_evals.py --filter flask,requests
  python evals/run_agent_evals.py --limit 1
  python evals/run_agent_evals.py --provider openrouter --model z-ai/glm-5.2:free --save saved/
  python evals/run_agent_evals.py --tasks evals/tasks_local.yaml   # 用本地/本项目仓库评测，免克隆
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from src.agent import CodebaseNavigator
from src.config import PROVIDERS, resolve_config
from src.repo import clone_repo


def _load_citation_module():
    """把 run_ai_evals.py 当模块加载，复用引用提取/三级解析，避免重复实现。"""
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("run_ai_evals", here / "run_ai_evals.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cite = _load_citation_module()


def _normalize_path(p: str, repo_path: Path) -> str:
    """工具参数里的路径可能是绝对路径，统一成仓库内相对路径（posix 分隔）。"""
    p = (p or "").replace("\\", "/")
    root = str(repo_path.resolve()).replace("\\", "/") + "/"
    if p.startswith(root):
        p = p[len(root):]
    return p.lstrip("/")


def _opened_files(calls: list[dict], repo_path: Path) -> set[str]:
    """Agent 真实打开过的文件（read_file / get_imports / get_function_signatures）。"""
    opened: set[str] = set()
    file_tools = {"read_file", "get_imports", "get_function_signatures"}
    for c in calls:
        if c.get("name") not in file_tools:
            continue
        fp = (c.get("args") or {}).get("file_path") or ""
        if fp:
            opened.add(_normalize_path(str(fp), repo_path))
    return opened


def _gold_hit(gold: str, candidates: set[str] | list[str]) -> bool:
    """金标文件是否命中（按完整路径或同 basename 唯一匹配）。"""
    g = gold.replace("/", os.sep)
    gname = Path(g).name
    for cand in candidates:
        cand_norm = str(cand).replace("/", os.sep)
        if cand_norm == g or cand_norm.endswith(os.sep + g) or Path(cand_norm).name == gname:
            return True
    return False


def evaluate_task(answer: str, calls: list[dict], repo_path: Path, task: dict) -> dict:
    """单个 Ask 任务的指标：成功率 / 金标触达 / 幻觉 / 工具数。"""
    gold_files = task.get("gold_files", [])
    gold_terms = task.get("gold_terms", [])

    resolved_ok: set[str] = set()
    hallucinated: list[str] = []
    for rel in dict.fromkeys(cite.extract_citations(answer or "")):
        resolved, status = cite._classify_citation(rel, repo_path)
        if status == "not_found":
            hallucinated.append(rel)
        elif resolved is not None:
            resolved_ok.add(str(resolved).replace("/", os.sep))

    answer_gold_hits = [g for g in gold_files if _gold_hit(g, resolved_ok)]
    opened = _opened_files(calls, repo_path)
    opened_gold_hits = [g for g in gold_files if _gold_hit(g, opened)]
    term_hits = [t for t in gold_terms if t and t in (answer or "")]

    citations = cite.extract_citations(answer or "")
    hallucination_rate = round(len(hallucinated) / len(set(citations)), 3) if citations else 0.0
    return {
        "ok": bool(answer_gold_hits) and not hallucinated,
        "gold_files_hit": answer_gold_hits,
        "gold_reached": opened_gold_hits,
        "gold_terms_hit": term_hits,
        "hallucinated_citations": hallucinated,
        "hallucination_rate": hallucination_rate,
        "tool_calls": len(calls),
        "answer_len": len(answer or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Codebase Navigator Agent 行为评测（Ask 任务）")
    parser.add_argument("--clone-dir", default=str(Path(__file__).parent / ".cache"))
    parser.add_argument("--tasks", default=str(Path(__file__).parent / "tasks.yaml"))
    parser.add_argument("--limit", type=int, help="只评测前 N 个仓库")
    parser.add_argument("--filter", help="按仓库名过滤（逗号分隔）")
    parser.add_argument("--provider", choices=list(PROVIDERS), default="openrouter")
    parser.add_argument("--model", help="模型名称")
    parser.add_argument("--save", help="保存回答原文到目录（便于人工核对）")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    with open(here / "repos.yaml", encoding="utf-8") as f:
        repo_urls = {r["name"]: r["url"] for r in yaml.safe_load(f)["repos"]}
    tasks_path = Path(args.tasks)
    if not tasks_path.exists():
        print(f"任务文件不存在：{tasks_path}")
        return 1
    with open(tasks_path, encoding="utf-8") as f:
        tasks = yaml.safe_load(f)["tasks"]
    if args.filter:
        wanted = {n.strip() for n in args.filter.split(",") if n.strip()}
        tasks = [t for t in tasks if t["repo"] in wanted]
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        print("没有匹配的任务（检查 --filter 是否在 tasks.yaml 里）。")
        return 1

    try:
        config = resolve_config(provider=args.provider, model=args.model)
    except Exception as e:
        print(f"❌ {e}")
        return 1

    cache_dir = Path(args.clone_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    save_dir = Path(args.save) if args.save else None
    print(f"模型：{config.provider} / {config.model}\n")

    results: list[dict] = []
    total_ok = total_tasks = 0
    for item in tasks:
        name = item["repo"]
        repo_path = (item.get("repo_path") or "").strip()
        if repo_path == "self":
            dest = here.parent  # 用本项目自己评测（dogfood）
        elif repo_path:
            dest = Path(repo_path).resolve()
            if not dest.exists():
                print(f"⚠ {name} 的本地路径不存在：{dest}，跳过")
                continue
        else:
            url = repo_urls.get(name)
            if not url:
                print(f"⚠ {name} 不在 evals/repos.yaml 且无 repo_path，跳过")
                continue
            dest = cache_dir / name
            print(f"▶ {name}：加载仓库...", flush=True)
            if not (dest.exists() and any(dest.iterdir())):
                try:
                    clone_repo(url, dest=dest)
                except Exception as e:
                    print(f"  ❌ 克隆失败：{e}")
                    continue
        print(f"▶ {name}：{dest}", flush=True)

        try:
            agent = CodebaseNavigator(str(dest), config)
        except Exception as e:
            print(f"  ❌ Agent 初始化失败：{e}")
            continue

        for qi, question in enumerate(item["questions"], 1):
            try:
                agent.reset_conversation()
                t0 = time.time()
                answer = agent.chat(question["q"])
                elapsed = time.time() - t0
                calls = agent.get_last_tool_calls()
                m = evaluate_task(answer, calls, dest, question)
            except Exception as e:
                print(f"  Q{qi} ❌ 执行失败：{e}")
                results.append({"repo": name, "q": question["q"], "ok": False, "error": str(e)})
                total_tasks += 1
                continue

            m.update({"repo": name, "q": question["q"], "elapsed": round(elapsed, 1)})
            results.append(m)
            total_tasks += 1
            total_ok += 1 if m["ok"] else 0
            hits = ",".join(Path(g).name for g in m["gold_files_hit"]) or "无"
            print(
                f"  Q{qi} {'✅' if m['ok'] else '❌'} 耗时{m['elapsed']}s "
                f"引用金标:{hits} 幻觉:{len(m['hallucinated_citations'])} "
                f"真实触达:{len(m['gold_reached'])} 工具:{m['tool_calls']}次"
            )
            if m["hallucinated_citations"]:
                print(f"    🚨 幻觉引用：{m['hallucinated_citations']}")
            if save_dir:
                save_dir.mkdir(parents=True, exist_ok=True)
                (save_dir / f"{name}_q{qi}.md").write_text(answer or "", encoding="utf-8")

    print(f"\n{'=' * 56}")
    print(f"Agent 行为评测完成：任务 {total_ok}/{total_tasks} 通过")
    for name in dict.fromkeys(r["repo"] for r in results):
        rs = [r for r in results if r["repo"] == name]
        okn = sum(1 for r in rs if r["ok"])
        tools = [r.get("tool_calls", 0) for r in rs]
        avg_tools = round(sum(tools) / len(tools), 1) if tools else 0
        hal = [r for r in rs if r.get("hallucinated_citations")]
        print(f"  {'✅' if okn == len(rs) else '❌'} {name}  {okn}/{len(rs)}"
              f"  平均工具调用 {avg_tools}  幻觉任务 {len(hal)}")

    out_path = cache_dir / "agent_eval_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n结果已保存：{out_path}")
    return 0 if total_tasks and total_ok == total_tasks else 1


if __name__ == "__main__":
    raise SystemExit(main())
