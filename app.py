"""Codebase Navigator Web 单页（Gradio）。

启动：
  python app.py
然后打开 http://127.0.0.1:7860

页面：
- 静态报告：无需 API Key，秒出
- AI 概览：需要 API Key（默认读 .env 的 DEEPSEEK_API_KEY）
- 问答：边读报告边追问，答案带 file:line 引用
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import gradio as gr

from src.agent import CodebaseNavigator
from src.config import PROVIDERS, resolve_config
from src.repo import load_repo
from src.report import generate_report

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 全局状态：当前加载的仓库与 Agent
state = {"repo_path": None, "agent": None}

# 常用模型快捷选项
MODEL_OPTIONS = {
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "openrouter": ["xiaomi/mimo-v2-flash:free", "google/gemma-3-4b-it:free"],
    "groq": ["llama-3.1-8b-instant"],
    "openai": ["gpt-4o-mini", "gpt-4o"],
}


def _cleanup_old_repo():
    """清理上一次加载的临时克隆目录。"""
    old = state["repo_path"]
    if old and str(old).startswith(tempfile.gettempdir()) and "codebase-nav-" in str(old):
        shutil.rmtree(old, ignore_errors=True)
    state["repo_path"] = None
    state["agent"] = None


def initialize(repo_input: str, provider: str, model: str, api_key: str) -> str:
    """加载仓库并初始化 Agent。"""
    if not repo_input or not repo_input.strip():
        return "❌ 请输入 GitHub URL 或本地路径。"
    try:
        config = resolve_config(
            provider=provider, model=model.strip() or None, api_key=api_key.strip() or None
        )
        _cleanup_old_repo()
        repo_path = load_repo(repo_input.strip())
        agent = CodebaseNavigator(str(repo_path), config)
        state["repo_path"] = repo_path
        state["agent"] = agent
        return (
            f"✅ 已加载仓库 **{repo_path.name}**\n\n"
            f"模型：`{config.model}`\n\n"
            f"现在可以：生成静态报告 → AI 概览 → 在问答里追问。"
        )
    except Exception as e:
        return f"❌ 加载失败：{e}"


def static_report() -> str:
    """静态学习报告（无需 LLM）。"""
    if not state["repo_path"]:
        return "请先在顶部加载仓库（GitHub URL 或本地路径）。"
    return generate_report(str(state["repo_path"]))


def ai_overview() -> tuple[str, str]:
    """AI 概览 + 工具调用轨迹。"""
    if not state["agent"]:
        return "请先加载仓库（AI 功能需要 API Key）。", ""
    try:
        text = state["agent"].get_overview()
        calls = state["agent"].get_last_tool_calls()
        trace = "\n".join(
            f"{i + 1}. `{c['name']}` args={c['args']}" for i, c in enumerate(calls)
        ) or "（无工具调用）"
        return text, trace
    except Exception as e:
        return f"❌ 生成失败：{e}", ""


def chat(message: str, history: list) -> tuple[list, str]:
    """边读边问。"""
    if not message.strip():
        return history, ""
    if not state["agent"]:
        answer = "请先加载仓库（AI 功能需要 API Key）。"
    else:
        try:
            answer = state["agent"].chat(message)
        except Exception as e:
            answer = f"❌ 出错了：{e}"
    history = history + [
        {"role": "user", "content": message},
        {"role": "assistant", "content": answer},
    ]
    return history, ""


def build_app() -> gr.Blocks:
    with gr.Blocks(title="Codebase Navigator") as app:
        gr.Markdown(
            """
            # 📚 Codebase Navigator

            **30 分钟读懂任意代码库。** 输入 GitHub URL 或本地路径，生成结构化学习报告，边读边问。

            **流程**：加载仓库 → 静态报告（免费）→ AI 概览 → 问答追问
            """
        )

        with gr.Row():
            repo_input = gr.Textbox(
                label="GitHub URL 或本地路径",
                placeholder="https://github.com/psf/requests 或 E:\\some\\repo",
                scale=4,
            )
            load_btn = gr.Button("🚀 加载仓库", variant="primary", scale=1)

        with gr.Row():
            provider = gr.Dropdown(
                choices=list(PROVIDERS), value="deepseek", label="LLM 提供商"
            )
            model = gr.Textbox(
                value="deepseek-chat",
                label="模型（留空用默认）",
                placeholder="deepseek-chat",
            )
            api_key = gr.Textbox(
                label="API Key（留空用 .env）",
                type="password",
                placeholder="留空则读取 .env",
            )

        status = gr.Markdown("👋 输入仓库后点击「加载仓库」开始。")

        with gr.Tabs():
            with gr.Tab("📊 静态报告（免费）"):
                report_btn = gr.Button("生成静态学习报告", variant="secondary")
                report_out = gr.Markdown()

            with gr.Tab("🤖 AI 概览"):
                with gr.Row():
                    overview_btn = gr.Button("生成 AI 概览", variant="primary")
                overview_out = gr.Markdown()
                with gr.Accordion("工具调用轨迹", open=False):
                    trace_out = gr.Markdown()

            with gr.Tab("💬 问答"):
                chatbot = gr.Chatbot(height=480, label="针对代码库提问")
                with gr.Row():
                    chat_input = gr.Textbox(
                        placeholder="例如：这个项目怎么跑起来？认证流程在哪里？核心模块有哪些？",
                        scale=4,
                    )
                    chat_btn = gr.Button("发送", variant="primary", scale=1)

        gr.Markdown(
            """
            ---
            **示例问题**
            - 这个项目是做什么的？技术栈是什么？
            - 入口点在哪里？怎么运行？
            - 认证 / 数据库 / API 路由分别在哪里实现？
            - 我应该按什么顺序读源码？

            **注意**：AI 功能需要 API Key（默认读取 `.env` 的 `DEEPSEEK_API_KEY`，也可在页面上临时填写）。
            """
        )

        load_btn.click(initialize, [repo_input, provider, model, api_key], status)
        report_btn.click(static_report, None, report_out)
        overview_btn.click(ai_overview, None, [overview_out, trace_out])
        chat_btn.click(chat, [chat_input, chatbot], [chatbot, chat_input])
        chat_input.submit(chat, [chat_input, chatbot], [chatbot, chat_input])

    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Codebase Navigator Web")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    build_app().launch(server_name="127.0.0.1", server_port=args.port)