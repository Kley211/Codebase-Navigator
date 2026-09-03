"""Codebase Navigator Web 单页（Gradio）。

启动：
  python app.py
然后打开 http://127.0.0.1:7860

页面：
- 静态报告：无需 API Key，秒出
- AI 概览：需要 API Key（默认读 .env 的 OPENROUTER_API_KEY，免费模型 z-ai/glm-5.2:free）
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
from src.progress import ProgressStore

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 全局状态：当前加载的仓库与 Agent
state = {"repo_path": None, "agent": None}
progress_store = ProgressStore()

# 常用模型快捷选项
MODEL_OPTIONS = {
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "openrouter": ["z-ai/glm-5.2:free", "z-ai/glm-5.2", "z-ai/glm-4.7"],
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
            f"现在可以：静态报告 → AI 概览 → 问答追问 → 学习进度勾选清单。"
        )
    except Exception as e:
        return f"❌ 加载失败：{e}"


def _progress_repo_key() -> str | None:
    """当前仓库的学习进度 key（用仓库名）。"""
    repo_path = state["repo_path"]
    return Path(repo_path).name if repo_path else None


def progress_refresh() -> tuple[list[str], str]:
    """加载当前仓库的学习清单（首次自动生成默认清单）。"""
    key = _progress_repo_key()
    if not key or not state["repo_path"]:
        return [], "请先加载仓库（GitHub URL 或本地路径）。"
    progress_store.ensure(key, str(state["repo_path"]))
    items = progress_store.items(key)
    done = progress_store.done(key)
    pct = int(len(done) / len(items) * 100) if items else 0
    return done, f"「{key}」 已完成 {len(done)}/{len(items)} · {pct}%"


def progress_toggle(done: list[str]) -> str:
    """勾选/取消后保存进度。"""
    key = _progress_repo_key()
    if not key:
        return "请先加载仓库。"
    done = list(done or [])
    progress_store.update(key, done)
    items = progress_store.items(key)
    pct = int(len(done) / len(items) * 100) if items else 0
    return f"「{key}」 已完成 {len(done)}/{len(items)} · {pct}%"


def progress_reset() -> tuple[list[str], str]:
    """重置当前仓库的学习进度。"""
    key = _progress_repo_key()
    if not key:
        return [], "请先加载仓库。"
    progress_store.reset(key)
    return [], f"已重置「{key}」的学习进度，重新开始！"


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
        trace = f"**实际使用模型**：`{state['agent'].active_model}`\n\n{trace}"
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
                choices=list(PROVIDERS), value="openrouter", label="LLM 提供商"
            )
            model = gr.Textbox(
                value="z-ai/glm-5.2:free",
                label="模型（留空用默认）",
                placeholder="z-ai/glm-5.2:free",
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

            with gr.Tab("📈 学习进度"):
                gr.Markdown(
                    "勾选已完成的学习项，进度自动保存到本地 `~/.codebase-navigator/progress.json`，下次打开仍然保留。"
                )
                with gr.Row():
                    progress_load_btn = gr.Button("📋 加载学习清单", variant="secondary")
                    progress_reset_btn = gr.Button("↺ 重置进度", variant="stop")
                progress_group = gr.CheckboxGroup(label="学习清单（点击勾选）", choices=[])
                progress_label = gr.Label("加载仓库后点击「加载学习清单」。")

        gr.Markdown(
            """
            ---
            **示例问题**
            - 这个项目是做什么的？技术栈是什么？
            - 入口点在哪里？怎么运行？
            - 认证 / 数据库 / API 路由分别在哪里实现？
            - 我应该按什么顺序读源码？

            **注意**：AI 功能需要 API Key（默认读取 `.env` 的 `OPENROUTER_API_KEY`，使用免费模型 `z-ai/glm-5.2:free`；也可在页面上临时填写）。
            """
        )

        load_btn.click(initialize, [repo_input, provider, model, api_key], status)
        report_btn.click(static_report, None, report_out)
        overview_btn.click(ai_overview, None, [overview_out, trace_out])
        chat_btn.click(chat, [chat_input, chatbot], [chatbot, chat_input])
        chat_input.submit(chat, [chat_input, chatbot], [chatbot, chat_input])
        load_btn.click(progress_refresh, None, [progress_group, progress_label])
        progress_load_btn.click(progress_refresh, None, [progress_group, progress_label])
        progress_reset_btn.click(progress_reset, None, [progress_group, progress_label])
        progress_group.select(progress_toggle, progress_group, progress_label)

    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Codebase Navigator Web")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    build_app().launch(server_name="127.0.0.1", server_port=args.port)
