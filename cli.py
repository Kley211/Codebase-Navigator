"""Codebase Navigator 命令行入口。

用法示例：
  python cli.py <仓库URL或路径> --report            静态学习报告（无需 API Key）
  python cli.py <仓库URL或路径> --overview          AI 概览（需要 API Key）
  python cli.py <仓库URL或路径> --ask "问题"         提问
"""

from __future__ import annotations

import argparse
import sys
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

    if args.report or not (args.overview or args.ask):
        print("\n" + "=" * 60)
        report = generate_report(str(repo_path))
        print(report)
        if args.output:
            Path(args.output).write_text(report, encoding="utf-8")
            print(f"\n✅ 报告已保存：{args.output}")
        if not (args.overview or args.ask):
            print("\n提示：用 --overview 生成 AI 概览，用 --ask '你的问题' 提问。")
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

    if args.ask:
        print(f"\n🔍 问题：{args.ask}\n")
        print(agent.ask(args.ask))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())