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
from src.tutor import WebTutor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 全局状态：当前加载的仓库与 Agent
state = {"repo_path": None, "agent": None, "tutor": None, "tutor_plan": None}
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
    state["tutor"] = None
    state["tutor_plan"] = None


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
            f"现在可以：静态报告 → AI 概览 → 问答追问 → 学习进度 → **带读陪练**（Tab 05）。"
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
        return gr.update(choices=[], value=[]), "请先加载仓库（GitHub URL 或本地路径）。"
    progress_store.ensure(key, str(state["repo_path"]))
    items = progress_store.items(key)
    done = progress_store.done(key)
    pct = int(len(done) / len(items) * 100) if items else 0
    return gr.update(choices=items, value=done), f"「{key}」 已完成 {len(done)}/{len(items)} · {pct}%"


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
        return gr.update(choices=[], value=[]), "请先加载仓库。"
    items = progress_store.items(key)
    progress_store.reset(key)
    return gr.update(choices=items, value=[]), f"已重置「{key}」的学习进度，重新开始！"


def _default_plan_path() -> Path:
    """带读剧本默认路径：与 CLI `--tutor` 保持一致（learn-<仓库名>.md）。"""
    repo_path = state["repo_path"]
    if not repo_path:
        return Path.cwd() / "learn-plan.md"
    return Path.cwd() / f"learn-{Path(repo_path).name}.md"


def _tutor_start(regen: bool) -> tuple[list, str, str]:
    """启动带读陪练：复用/生成剧本 → 输出 RoadMap 总览 + 开场引导。"""
    agent = state["agent"]
    if not agent or not state["repo_path"]:
        return [], "请先加载仓库（带读陪练是 AI 功能，需要 API Key）。", ""
    try:
        plan_text = state["tutor_plan"]
        plan_path = _default_plan_path()
        if not plan_text or regen:
            if not regen and plan_path.exists():
                plan_text = plan_path.read_text(encoding="utf-8")
            else:
                plan_text = agent.get_learn_plan()
                plan_path.write_text(plan_text, encoding="utf-8")
            state["tutor_plan"] = plan_text
        tutor = WebTutor(agent, plan_text)
        state["tutor"] = tutor
        roadmap = tutor.roadmap_md()
        history = [{"role": "assistant", "content": m} for m in tutor.start()]
        return history, tutor.summary(), roadmap
    except Exception as e:
        state["tutor"] = None
        plan_name = Path(state["repo_path"]).name if state["repo_path"] else "仓库名"
        note = (
            f"❌ 启动带读失败：{e}\n\n"
            f"可稍后重试；若多次失败，先用 CLI 生成剧本：`python cli.py <仓库> --learn`，"
            f"再把生成的 `learn-{plan_name}.md` 放到项目目录，最后点「开始带读」。"
        )
        return [{"role": "assistant", "content": note}], "启动失败", ""


def tutor_start() -> tuple[list, str, str]:
    """「开始带读」：优先复用已有剧本，秒开新会话。"""
    return _tutor_start(regen=False)


def tutor_regen() -> tuple[list, str, str]:
    """「重新生成剧本」：忽略旧剧本，让 AI 重新生成一份。"""
    return _tutor_start(regen=True)


def tutor_reset(roadmap: str = "") -> tuple[list, str, str]:
    """「结束会话」：结束当前会话，保留 RoadMap 与剧本供下次秒开。"""
    state["tutor"] = None
    return [], "会话已结束。点「开始带读」再来一轮（沿用已生成的剧本）。", roadmap


def tutor_send(message: str, history: list) -> tuple[list, str, str]:
    """带读会话逐轮应答：学习者回复 → 苏格拉底判定/追问 → 追加消息。"""
    message = (message or "").strip()
    tutor = state["tutor"]
    if not tutor:
        note = "请先点「开始带读」启动会话。"
        history = history or []
        if message:
            history = history + [
                {"role": "user", "content": message},
                {"role": "assistant", "content": note},
            ]
        return history, "未开始", ""
    msgs = tutor.respond(message)
    history = history or []
    if message:
        history = history + [{"role": "user", "content": message}]
    history = history + [{"role": "assistant", "content": m} for m in msgs]
    return history, tutor.summary(), ""


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


# ---------------------------------------------------------------
# Web 视觉体系：「NIGHT RADAR 深读雷达」
# 设计实现集中在 web/style.css（本文件读取后随页面 <style> 注入）。
# ---------------------------------------------------------------
WEB_CSS = (Path(__file__).resolve().parent / "web" / "style.css").read_text(encoding="utf-8")

_HERO_HTML = """
<div class="cn-hero">
  <div class="cn-rail">
    <span class="cn-mark">&#9670;</span>
    <span class="cn-kicker">Codebase Field Manual</span>
    <span class="cn-coords">night session &middot; 30-min read</span>
  </div>
  <h1 class="cn-title">Codebase <span class="cn-brand-amber">Navigator</span></h1>
  <p class="cn-sub">30 分钟读懂任意开源代码库 &mdash;&mdash; 不靠逐行硬啃，而是按<em>地图式路线</em>读：先整体、再架构、后模块、最后深入源码。</p>
  <div class="cn-route">
    <span class="cn-way"><span>01</span>Load<small>载入</small></span>
    <span class="cn-arrow">&rarr;</span>
    <span class="cn-way"><span>02</span>Read<small>报告</small></span>
    <span class="cn-arrow">&rarr;</span>
    <span class="cn-way"><span>03</span>Ask<small>问答</small></span>
    <span class="cn-arrow">&rarr;</span>
    <span class="cn-way"><span>04</span>Track<small>进度</small></span>
    <span class="cn-arrow">&rarr;</span>
    <span class="cn-way"><span>05</span>Coach<small>带读</small></span>
  </div>
</div>
"""

_FOOTER_HTML = """
<div class="cn-footer">
  <div class="cn-foot-head"><span>&#9670; Quick starts</span><span>&mdash;&mdash; 拿到报告后可以这样问</span></div>
  <div class="cn-chips">
    <span class="cn-chip">这个项目是做什么的？技术栈是什么？</span>
    <span class="cn-chip">入口点在哪里？怎么运行？</span>
    <span class="cn-chip">认证 / 数据库 / API 路由在哪里实现？</span>
    <span class="cn-chip">我应该按什么顺序读源码？</span>
  </div>
  <p class="cn-note"><b>AI 功能说明：</b>默认读取 <code>.env</code> 的 <code>OPENROUTER_API_KEY</code>（免费模型 <code>z-ai/glm-5.2:free</code>），也可在页面上临时填写；学习进度保存在 <code>~/.codebase-navigator/progress.json</code>。</p>
</div>
"""


def build_app() -> gr.Blocks:
    with gr.Blocks(title="Codebase Navigator · 源码领航") as app:
        gr.HTML("<style>\n" + WEB_CSS + "\n</style>\n" + _HERO_HTML, elem_id="cn-top")

        with gr.Row(elem_id="console-row"):
            repo_input = gr.Textbox(
                label="GitHub URL / 本地路径",
                placeholder="https://github.com/pallets/flask  或  E:\\some\\repo",
                scale=4,
                elem_id="repo-input",
            )
            load_btn = gr.Button("加载仓库", variant="primary", scale=1, elem_id="load-btn")

        with gr.Row(elem_id="cfg-row"):
            provider = gr.Dropdown(
                choices=list(PROVIDERS), value="openrouter", label="LLM 提供商",
                elem_id="cfg-provider",
            )
            model = gr.Textbox(
                value="z-ai/glm-5.2:free",
                label="模型",
                placeholder="z-ai/glm-5.2:free",
                elem_id="cfg-model",
            )
            api_key = gr.Textbox(
                label="API Key（留空用 .env）",
                type="password",
                placeholder="留空则读取 .env",
                elem_id="cfg-key",
            )

        status = gr.Markdown(
            "`[ idle ]` 粘贴 GitHub 地址或本地目录，点「加载仓库」开始。静态报告免费秒出，AI 能力需要 API Key。",
            elem_id="status",
        )

        with gr.Tabs(elem_id="deck"):
            with gr.Tab("01 · 静态报告"):
                report_btn = gr.Button("生成静态学习报告", variant="secondary", elem_id="report-btn")
                report_out = gr.Markdown(elem_id="report-out")

            with gr.Tab("02 · AI 概览"):
                overview_btn = gr.Button("生成 AI 概览", variant="primary", elem_id="overview-btn")
                overview_out = gr.Markdown(elem_id="overview-out")
                with gr.Accordion("工具调用轨迹", open=False):
                    trace_out = gr.Markdown(elem_id="trace-out")

            with gr.Tab("03 · 源码问答"):
                chatbot = gr.Chatbot(height=480, label="针对代码库提问", elem_id="chatbot")
                with gr.Row(elem_id="chat-row"):
                    chat_input = gr.Textbox(
                        placeholder="例如：这个项目怎么跑起来？认证流程在哪里？核心模块有哪些？",
                        scale=4,
                        elem_id="chat-input",
                    )
                    chat_btn = gr.Button("发送", variant="primary", scale=1, elem_id="chat-send")

            with gr.Tab("04 · 学习进度"):
                gr.Markdown(
                    "勾选已完成的学习项，进度自动保存到本地 `~/.codebase-navigator/progress.json`，下次打开仍然保留。"
                )
                with gr.Row(elem_id="progress-actions"):
                    progress_load_btn = gr.Button("加载学习清单", variant="secondary", elem_id="progress-load")
                    progress_reset_btn = gr.Button("重置进度", variant="stop", elem_id="progress-reset")
                progress_group = gr.CheckboxGroup(
                    label="学习清单 · 点击勾选已完成项", choices=[], elem_id="progress-group"
                )
                progress_label = gr.Markdown(
                    "加载仓库后点击「加载学习清单」。", elem_id="progress-status"
                )

            with gr.Tab("05 · 带读陪练"):
                gr.Markdown(
                    "**先看 RoadMap 认清全程，再按步骤带读**：每步 精读 → 苏格拉底自检 →（可选）动手 → 毕业关卡。"
                    "验收 = **能复述 + 能讲**（能改是加分项，动手可 `跳过`）。"
                    "第一次点「开始带读」会用 AI 生成剧本（约 1-2 分钟），之后秒开。"
                )
                with gr.Row(elem_id="tutor-actions"):
                    tutor_start_btn = gr.Button("开始带读", variant="primary", elem_id="tutor-start")
                    tutor_regen_btn = gr.Button("重新生成剧本", variant="secondary", elem_id="tutor-regen")
                    tutor_stop_btn = gr.Button("结束会话", variant="stop", elem_id="tutor-stop")
                tutor_roadmap = gr.Markdown(
                    "（还没有路线图。点「开始带读」会先生成 RoadMap，再逐步带读。）",
                    elem_id="tutor-roadmap",
                )
                tutor_summary = gr.Markdown(
                    "尚未开始。加载仓库后点「开始带读」。", elem_id="tutor-summary"
                )
                tutor_chat = gr.Chatbot(
                    height=520,
                    label="带读陪练会话 · 读代码 / 自检 / 动手汇报",
                    elem_id="tutor-chat",
                )
                with gr.Row(elem_id="tutor-send-row"):
                    tutor_input = gr.Textbox(
                        placeholder="读完输入 go 开始自检；直接回答追问；不懂可输入 问：你的问题；动手可 跳过；结束输入 退出。",
                        scale=4,
                        elem_id="tutor-input",
                    )
                    tutor_send_btn = gr.Button("发送", variant="primary", scale=1, elem_id="tutor-send")

        gr.HTML(_FOOTER_HTML, elem_id="cn-footer")

        load_btn.click(initialize, [repo_input, provider, model, api_key], status)
        report_btn.click(static_report, None, report_out)
        overview_btn.click(ai_overview, None, [overview_out, trace_out])
        chat_btn.click(chat, [chat_input, chatbot], [chatbot, chat_input])
        chat_input.submit(chat, [chat_input, chatbot], [chatbot, chat_input])
        load_btn.click(progress_refresh, None, [progress_group, progress_label])
        progress_load_btn.click(progress_refresh, None, [progress_group, progress_label])
        progress_reset_btn.click(progress_reset, None, [progress_group, progress_label])
        progress_group.change(progress_toggle, progress_group, progress_label)
        tutor_start_btn.click(tutor_start, None, [tutor_chat, tutor_summary, tutor_roadmap])
        tutor_regen_btn.click(tutor_regen, None, [tutor_chat, tutor_summary, tutor_roadmap])
        tutor_stop_btn.click(tutor_reset, [tutor_roadmap], [tutor_chat, tutor_summary, tutor_roadmap])
        tutor_send_btn.click(
            tutor_send, [tutor_input, tutor_chat], [tutor_chat, tutor_summary, tutor_input]
        )
        tutor_input.submit(
            tutor_send, [tutor_input, tutor_chat], [tutor_chat, tutor_summary, tutor_input]
        )

    return app


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Codebase Navigator Web")
    parser.add_argument("--port", type=int, default=7860)
    args = parser.parse_args()
    build_app().launch(server_name="127.0.0.1", server_port=args.port)
