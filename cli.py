"""Codebase Navigator 命令行入口。

用法示例：
  python cli.py <仓库URL或路径> --report            静态学习报告（无需 API Key）
  python cli.py <仓库URL或路径> --overview          AI 概览（需要 API Key）
  python cli.py <仓库URL或路径> --ask "问题"         提问
  python cli.py <仓库URL或路径> --learn            带读剧本（需要 API Key）
  python cli.py <仓库URL或路径> --tutor            AI 带读陪练（苏格拉底式逐问判定）
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from src.config import PROVIDERS, resolve_config
from src.repo import load_repo
from src.report import generate_report
from src.agent import CodebaseNavigator


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codebase-navigator",
        description="Codebase Navigator：30 分钟读懂任意代码库",
    )
    parser.add_argument("repo", help="GitHub URL（如 https://github.com/psf/requests）或本地路径")
    parser.add_argument("--report", action="store_true", help="生成静态学习报告（无需 API Key）")
    parser.add_argument("--overview", action="store_true", help="生成 AI 代码库概览（需要 API Key）")
    parser.add_argument("--ask", metavar="QUESTION", help="向 AI 提问")
    parser.add_argument("--learn", action="store_true", help="生成「带读剧本」：5-8 步可自检的学习计划（需要 API Key）")
    parser.add_argument("--tutor", action="store_true", help="启动「带读陪练」：逐问苏格拉底式追问并判定理解（需要 API Key）")
    parser.add_argument("--plan", metavar="PATH", help="--tutor 使用已有剧本文件（默认复用/生成 learn-<仓库名>.md）")
    parser.add_argument("--provider", choices=list(PROVIDERS), help="LLM 提供商")
    parser.add_argument("--model", help="模型名称（默认按提供商选择）")
    parser.add_argument("--api-key", help="API Key（也可用环境变量）")
    parser.add_argument("--output", help="把报告/回答写入文件")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        repo_path = load_repo(args.repo)
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        return 1

    print(f"📁 仓库：{repo_path}")

    if args.report or not (args.overview or args.ask or args.learn or args.tutor):
        print("\n" + "=" * 60)
        report = generate_report(str(repo_path))
        print(report)
        if args.output:
            Path(args.output).write_text(report, encoding="utf-8")
            print(f"\n✅ 报告已保存：{args.output}")
        if not (args.overview or args.ask or args.learn or args.tutor):
            print("\n提示：用 --overview 生成 AI 概览，用 --ask '你的问题' 提问，用 --learn 生成带读剧本，用 --tutor 进入带读陪练。")
            return 0

    try:
        config = resolve_config(provider=args.provider, model=args.model, api_key=args.api_key)
        agent = CodebaseNavigator(str(repo_path), config)
    except Exception as e:
        print(f"\n❌ {e}", file=sys.stderr)
        print("提示：静态报告不需要 API Key，可运行：python cli.py <仓库> --report")
        return 1

    if args.overview:
        print("\n🔍 正在生成 AI 概览（可能需要一两分钟）...\n")
        text = agent.get_overview()
        print(text)
        if args.output:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"\n✅ 已保存：{args.output}")
        return 0

    if args.learn:
        print("\n🎓 正在设计「带读剧本」（面向会装环境、但没读过开源源码的新手）...\n")
        text = agent.get_learn_plan()
        print(text)
        if not args.output:
            # 默认存一份，方便照着做
            default_out = Path.cwd() / f"learn-{repo_path.name}.md"
            default_out.write_text(text, encoding="utf-8")
            print(f"\n✅ 剧本已保存：{default_out}")
        else:
            Path(args.output).write_text(text, encoding="utf-8")
            print(f"\n✅ 剧本已保存：{args.output}")
        if Path(repo_path).name.startswith("codebase-nav-") or str(repo_path).startswith(tempfile.gettempdir()):
            print(
                "\n提示：URL 仓库被克隆在临时目录用于分析，动手实验请在你自己的工作副本进行：\n"
                f"  git clone {args.repo}"
            )
        return 0

    if args.tutor:
        from src.tutor import run_tutor_loop

        plan_path = Path(args.plan) if args.plan else Path.cwd() / f"learn-{repo_path.name}.md"
        if plan_path.exists():
            plan_text = plan_path.read_text(encoding="utf-8")
            print(f"📖 使用已有剧本：{plan_path}")
        else:
            print("\n🎓 尚未找到剧本，正在生成（可能需要一两分钟）...")
            plan_text = agent.get_learn_plan()
            plan_path.write_text(plan_text, encoding="utf-8")
            print(f"✅ 剧本已保存：{plan_path}")
        print("\n💬 进入带读陪练……（阅读完成后输入 go；随时可输入 提示 / 退出）")
        result = run_tutor_loop(agent, plan_text)
        if result == "aborted":
            print(f"\n会话已退出。剧本保留在 {plan_path}，重新运行 --tutor 即可再来。")
            return 0
        if result == "failed":
            return 1
        print("\n🎉 会话完成。复述、能改、能讲——你已经真正读过这个项目了。")
        return 0

    if args.ask:
        print(f"\n🔍 问题：{args.ask}\n")
        print(agent.ask(args.ask))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
