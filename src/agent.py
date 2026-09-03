"""Agent 主循环：OpenAI 兼容 API + 工具调用（不依赖 LangChain/LangGraph）。

设计要点（面试可讲）：
- 自己实现 ReAct 式循环：模型决定调用哪个工具 → 执行 → 结果回传 → 直到模型给出最终回答
- 确定性工具负责"读代码"，LLM 只负责"总结与推理"，降低幻觉
- 每次工具调用都被记录，供界面展示"Agent 做了什么"
- 三重收尾保障：工具预算上限强制无工具收尾、接近上限时提醒、最终回答质量校验（不合格要求重答）
- 检测模型用文本模拟工具调用（不解析脆弱的伪 XML），提醒后仍不改正则强制收尾
- 免费模型自动降级：主模型 429/过载时按备用链切换，成功后记住当前模型
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from openai import APIError, APIConnectionError, APITimeoutError, OpenAI, RateLimitError

from .config import LLMConfig
from .context import build_overview_context
from .prompts import (
    SINGLE_SHOT_PROMPT,
    SYSTEM_PROMPT,
    OVERVIEW_PROMPT,
    DEEP_DIVE_PROMPT,
    LEARN_PLAN_PROMPT,
)
from .tools import call_tool, get_tool_schemas

MAX_TOOL_ROUNDS = 35        # 最大 API 往返轮数
MAX_TOOL_CALLS = 30         # 最大工具调用次数（耗尽后强制无工具收尾）
URGE_ROUNDS_LEFT = 4        # 剩余轮数低于该值时提醒模型收尾
MAX_FINAL_RETRIES = 2       # 最终回答不合格时允许重试次数

# 用于校验最终回答是否"像正式回答"（含标题结构与 文件:行号 引用）
_CITE_RE = re.compile(r"[\w./\\-]+\.\w+:\d+")
_HEADING_RE = re.compile(r"^#{1,6}\s", re.M)

# 模型用文本模拟工具调用的特征标记（格式五花八门，检测到即处理）
_FAKE_MARKER_RE = re.compile(r"tool_calls|invoke name=|parameter name=|\uff5c")

# 免费模型（如 OpenRouter 共享池）偶发限流/过载：多轮尝试 + 跨模型降级
_PASS_DELAYS = (0, 8, 25)  # 每轮尝试前等待秒数（0 为立即）
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _is_retryable(exc: Exception) -> bool:
    """判断异常是否值得重试（限流/连接/服务器过载）。"""
    if isinstance(exc, (RateLimitError, APIConnectionError, APITimeoutError)):
        return True
    if isinstance(exc, APIError):
        return getattr(exc, "status_code", None) in _RETRYABLE_STATUS
    return False


def _overview_final_check(content: str) -> bool:
    """AI 概览的最终回答校验：长度充足、有 Markdown 标题、有 file:line 引用。"""
    text = content.strip()
    return len(text) >= 200 and bool(_HEADING_RE.search(text)) and bool(_CITE_RE.search(text))


class CodebaseNavigator:
    """面向单个仓库的学习 Agent。"""

    def __init__(self, repo_path: str, config: LLMConfig, max_tool_rounds: int = MAX_TOOL_ROUNDS):
        self.repo_path = str(Path(repo_path).resolve())
        if not Path(self.repo_path).exists():
            raise ValueError(f"仓库路径不存在：{self.repo_path}")
        self.config = config
        self.max_tool_rounds = max_tool_rounds
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url)
        self.conversation: list[dict] = []
        self.last_tool_calls: list[dict] = []
        # 模型降级链：主模型在前，备用模型在后；active_model 记录实际生效的模型
        self.models = [config.model, *config.fallback_models]
        self.active_model = config.model

    def _complete(self, messages: list[dict], **kwargs):
        """带自动降级的补全调用。

        免费模型共享池偶发 429/过载：从当前模型开始多轮尝试，
        失败则切到下一个备用模型；成功后记住该模型避免反复试错。
        """
        start = self.models.index(self.active_model)
        ordered = self.models[start:] + self.models[:start]
        last_exc: Exception | None = None

        for delay in _PASS_DELAYS:
            if delay:
                time.sleep(delay)
            for model in ordered:
                try:
                    response = self.client.chat.completions.create(
                        model=model, messages=messages, **kwargs
                    )
                except Exception as exc:
                    last_exc = exc
                    if not _is_retryable(exc):
                        raise
                    continue
                if model != self.active_model:
                    self.active_model = model
                return response

        raise last_exc if last_exc is not None else RuntimeError("LLM 调用无响应")

    def _system_message(self) -> dict:
        return {"role": "system", "content": SYSTEM_PROMPT.format(repo_path=self.repo_path)}

    def _run(self, user_message: str, final_check=None) -> str:
        """执行一轮 Agent 循环。final_check 用于校验最终回答质量。"""
        self.conversation.append({"role": "user", "content": user_message})
        messages = [self._system_message(), *self.conversation]
        self.last_tool_calls = []
        urged = False
        forced_final = False
        retries = 0
        text_tool_nudged = False

        for round_index in range(self.max_tool_rounds):
            # 工具预算耗尽：去掉 tools，强制模型基于已有信息收尾
            if len(self.last_tool_calls) >= MAX_TOOL_CALLS and not forced_final:
                forced_final = True
                messages.append({
                    "role": "system",
                    "content": "工具调用次数已达上限。请基于已收集的信息立即输出最终回答，不要再调用工具。",
                })
                response = self._complete(messages, temperature=0)
                content = response.choices[0].message.content or "（模型未返回内容）"
                self.conversation.append({"role": "assistant", "content": content})
                return content

            response = self._complete(messages, tools=get_tool_schemas(), temperature=0)
            message = response.choices[0].message
            if message.tool_calls:
                # 接近轮数上限时提醒模型直接收尾
                if self.max_tool_rounds - round_index <= URGE_ROUNDS_LEFT and not urged:
                    urged = True
                    messages.append({
                        "role": "system",
                        "content": "你已经收集了足够的信息。请立即停止调用工具，基于已有信息输出最终回答。",
                    })
                messages.append(message)  # AI 消息（含工具调用）回传给模型
                for tc in message.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = call_tool(name, args)
                    self.last_tool_calls.append({"name": name, "args": args})
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": result[:6000]}
                    )
                continue

            content = message.content or "（模型未返回内容）"

            # 检测到文本模拟工具调用：第一次提醒，第二次强制收尾
            if _FAKE_MARKER_RE.search(content):
                messages.append(message)
                if not text_tool_nudged:
                    text_tool_nudged = True
                    messages.append({
                        "role": "system",
                        "content": "检测到你用文本形式模拟了工具调用。请改用 tools API 正式发起工具调用；若信息已足够，直接输出最终回答，不要再输出任何 XML 或标签。",
                    })
                    continue
                forced_final = True
                messages.append({
                    "role": "system",
                    "content": "请直接输出最终回答，不要再输出任何工具调用标签。",
                })
                response = self._complete(messages, temperature=0)
                content = response.choices[0].message.content or "（模型未返回内容）"
                self.conversation.append({"role": "assistant", "content": content})
                return content

            # 最终回答质量校验：不合格则要求重答
            if final_check is not None and not final_check(content) and retries < MAX_FINAL_RETRIES:
                retries += 1
                messages.append(message)
                messages.append({
                    "role": "system",
                    "content": "你刚才的输出不是正式回答（缺少报告结构或 file:line 引用）。请立即基于已有信息输出完整的最终回答，禁止描述思考过程。",
                })
                continue

            self.conversation.append({"role": "assistant", "content": content})
            return content

        return "已达到最大工具调用轮数，问题较复杂，请拆分后重试。"

    def get_overview(self) -> str:
        """生成代码库学习概览（单次生成：静态上下文 + 一次 LLM 调用）。

        大仓库下多轮 ReAct 容易让模型把工具调用输出成文本导致死循环，
        因此概览改为由确定性静态分析提供上下文、LLM 只做一次总结。
        """
        context = build_overview_context(self.repo_path)
        prompt = SINGLE_SHOT_PROMPT.format(repo_name=Path(self.repo_path).name, context=context)
        self.last_tool_calls = []
        self.conversation.append({"role": "user", "content": prompt})
        messages = [
            {"role": "system", "content": "你是代码库学习助手。直接输出最终报告，不要输出思考过程。"},
            *self.conversation,
        ]

        for _ in range(1 + MAX_FINAL_RETRIES):
            response = self._complete(messages, temperature=0, max_tokens=8192)
            content = response.choices[0].message.content or "（模型未返回内容）"
            if _overview_final_check(content):
                self.conversation.append({"role": "assistant", "content": content})
                return content
            messages.append({
                "role": "system",
                "content": "你刚才的输出不合格：缺少 `路径:行号` 形式的引用。请保持内容不变，为每个关键结论补充引用，行号只能从「核心文件」的编号行中选取。示例：`crates/uv/src/lib.rs:42`。整份报告必须包含至少 12 处 `路径:行号` 引用，否则仍不合格。直接输出重写后的完整报告，禁止输出思考过程。",
            })

        self.conversation.append({"role": "assistant", "content": content})
        return content

    def ask(self, question: str) -> str:
        """针对代码库提问。"""
        return self._run(DEEP_DIVE_PROMPT.format(question=question))

    def get_learn_plan(self) -> str:
        """生成「带读剧本」：把仓库转成 5-8 步、可自检的学习计划。

        与概览一致采用「静态上下文 + 一次 LLM 调用」，避免大仓库下
        多轮工具调用失控；结构不合格时自动让模型重写（最多重试 2 次）。
        """
        from .learn import validate_learn_plan, retry_hint

        context = build_overview_context(self.repo_path)
        prompt = LEARN_PLAN_PROMPT.format(
            repo_name=Path(self.repo_path).name, context=context
        )
        self.last_tool_calls = []
        self.conversation.append({"role": "user", "content": prompt})
        messages = [
            {"role": "system", "content": "你是代码库学习导师。直接输出完整剧本，不要输出思考过程。"},
            *self.conversation,
        ]
        content = ""
        for _ in range(1 + MAX_FINAL_RETRIES):
            response = self._complete(messages, temperature=0, max_tokens=8192)
            content = response.choices[0].message.content or "（模型未返回内容）"
            ok, problems = validate_learn_plan(content)
            if ok:
                self.conversation.append({"role": "assistant", "content": content})
                return content
            messages.append({
                "role": "system",
                "content": retry_hint(problems),
            })
        self.conversation.append({"role": "assistant", "content": content})
        return content

    def direct(self, system: str, user: str, temperature: float = 0.2, max_tokens: int = 800) -> str:
        """单轮直答（不带工具循环），供带读陪练的判定/追问/提示使用。"""
        response = self._complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content or ""

    def chat(self, message: str) -> str:
        """自由对话。"""
        return self._run(message)

    def reset_conversation(self) -> None:
        self.conversation = []
        self.last_tool_calls = []

    def get_last_tool_calls(self) -> list[dict]:
        return self.last_tool_calls
