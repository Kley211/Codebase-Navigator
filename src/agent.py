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
    ASK_PLAN_SYS,
    ASK_PLAN_USER,
    SINGLE_SHOT_PROMPT,
    SYSTEM_PROMPT,
    OVERVIEW_PROMPT,
    DEEP_DIVE_PROMPT,
    LEARN_PLAN_PROMPT,
    ARCH_DIAGRAM_PROMPT,
)
from .tools import call_tool, get_tool_schemas

MAX_TOOL_ROUNDS = 35        # 最大 API 往返轮数
MAX_TOOL_CALLS = 30         # 最大工具调用次数（耗尽后强制无工具收尾）
ASK_TOOL_BUDGET = 20        # Ask 单问工具预算（低于全局上限：逼模型按计划收尾，避免通读）
URGE_ROUNDS_LEFT = 4        # 剩余轮数低于该值时提醒模型收尾
MAX_FINAL_RETRIES = 2       # 最终回答不合格时允许重试次数

# 用于校验最终回答是否"像正式回答"（含标题结构与 文件:行号 引用）
_CITE_RE = re.compile(r"[\w./\\-]+\.\w+:\d+")
_HEADING_RE = re.compile(r"^#{1,6}\s", re.M)

# 模型用文本模拟工具调用的特征标记（格式五花八门，检测到即处理）
_FAKE_MARKER_RE = re.compile(r"tool_calls|invoke name=|parameter name=|\uff5c")
_ASK_STEP_RE = re.compile(r"^\s*(?:[-*•]|\d+\s*[.、)])\s*(.*)$")
_INJECTED_NOISE = (
    "工具调用次数已达上限",
    "请基于已收集的信息立即输出最终回答",
    "请基于已收集的信息输出最终回答",
    "你刚才的最终回答缺少足够的",
    "请基于已收集的信息重写最终回答",
    "禁止再调用工具",
    "禁止输出任何工具调用标签",
    "请直接输出最终回答",
    "检测到你用文本形式模拟了工具调用",
    "不要输出思考过程",
)

_CITE_REWRITE_SYS = """你刚才的回答缺少 `完整相对路径:行号` 形式的引用（至少 2 条不重复）。
请基于已读到的内容直接重写最终回答：
- 每条结论后用括号附真实引用，示例 `（src/agent.py:168）`；禁止只写文件名不带行号。
- 保持回答自然、面向学习者，不要出现「引用」「file:line」等元描述。
- 不要再调用工具，不要复述任何提示语，直接输出回答正文。"""

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


def _ask_final_check(content: str) -> bool:
    """Ask 问答的最终回答校验：有实质内容，且至少 2 条不重复的 file:line 引用。"""
    text = (content or "").strip()
    if len(text) < 80:
        return False
    return len(set(_CITE_RE.findall(text))) >= 2


def _parse_ask_plan(text: str, max_steps: int = 5) -> list[str]:
    """把规划器输出解析成步骤列表（容忍编号/无序列表/多余标题等噪音）。"""
    lines: list[str] = []
    for raw in (text or "").splitlines():
        m = _ASK_STEP_RE.match(raw)
        if not m:
            continue
        step = m.group(1).strip()
        if step:
            lines.append(step[:200])
        if len(lines) >= max_steps:
            break
    return lines


def _strip_head_noise(content: str) -> str:
    """模型可能把系统收尾提示原样复述到回答开头，这里去掉这些噪声行。"""
    lines = (content or "").splitlines()
    while lines:
        head = lines[0].strip()
        if not head:
            lines.pop(0)
            continue
        if any(t in head for t in _INJECTED_NOISE):
            lines.pop(0)
            continue
        break
    return "\n".join(lines).strip() or (content or "")


_EMPTY_PLACEHOLDER = "（模型未返回内容）"


def _is_blank_answer(content: str) -> bool:
    """判断模型是否几乎没有输出（空串/占位符/一句话都没有）。"""
    text = _strip_head_noise(content or "").strip()
    return not text or text == _EMPTY_PLACEHOLDER or len(text) < 10


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
        self.last_plan: list[str] = []
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

    def _rewrite_until_cited(self, content: str, messages: list, final_check) -> str:
        """无工具重写直到带够引用（最多 1+MAX_FINAL_RETRIES 次），兜底防模型漏引用/复述提示语。"""
        content = _strip_head_noise(content or "")
        if final_check is None or final_check(content):
            return content
        for _ in range(1 + MAX_FINAL_RETRIES):
            messages.append({"role": "assistant", "content": content})
            if _is_blank_answer(content):
                messages.append({
                    "role": "system",
                    "content": "你刚才没有输出有效内容。请立即基于工具已返回的结果，用中文输出至少 3 句话的回答，"
                    "可引用你读过的文件（`完整相对路径:行号`）。不要输出空内容。",
                })
            else:
                messages.append({"role": "system", "content": _CITE_REWRITE_SYS})
            try:
                response = self._complete(messages, temperature=0)
                content = _strip_head_noise(response.choices[0].message.content or _EMPTY_PLACEHOLDER)
            except Exception:
                break
            if final_check(content):
                break
        return content

    def _run(self, user_message: str, final_check=None, tool_budget: int = MAX_TOOL_CALLS) -> str:
        """执行一轮 Agent 循环。final_check 用于校验最终回答质量，tool_budget 控制单问工具次数。"""
        self.conversation.append({"role": "user", "content": user_message})
        messages = [self._system_message(), *self.conversation]
        self.last_tool_calls = []
        urged = False
        budget_urged = False
        forced_final = False
        retries = 0
        text_tool_nudged = False

        for round_index in range(self.max_tool_rounds):
            # 工具预算耗尽：去掉 tools，强制模型基于已有信息收尾
            if len(self.last_tool_calls) >= tool_budget and not forced_final:
                forced_final = True
                messages.append({
                    "role": "system",
                    "content": "工具调用次数已达上限。请基于已收集的信息立即输出最终回答，不要再调用工具；不要复述本提示。",
                })
                response = self._complete(messages, temperature=0)
                content = response.choices[0].message.content or _EMPTY_PLACEHOLDER
                content = self._rewrite_until_cited(content, messages, final_check)
                self.conversation.append({"role": "assistant", "content": content})
                return content

            # 临近工具预算：先提醒模型收敛，避免打满后仍在读文件导致收尾困难
            if (
                tool_budget
                and len(self.last_tool_calls) >= tool_budget - 5
                and not budget_urged
            ):
                budget_urged = True
                messages.append({
                    "role": "system",
                    "content": f"你已接近本问的工具预算（{len(self.last_tool_calls)}/{tool_budget}）。"
                    "若核心问题已能回答，请立即输出最终回答；若还差关键文件，只读最关键的一个后收尾。",
                })

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

            content = message.content or _EMPTY_PLACEHOLDER

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
                content = response.choices[0].message.content or _EMPTY_PLACEHOLDER
                content = _strip_head_noise(content)
                self.conversation.append({"role": "assistant", "content": content})
                return content

            # 最终回答质量校验：不合格则要求重答
            if final_check is not None and not final_check(content):
                messages.append(message)
                if retries < MAX_FINAL_RETRIES:
                    retries += 1
                    messages.append({
                        "role": "system",
                        "content": "你刚才的输出不是正式回答（缺少报告结构或 file:line 引用）。请立即基于已有信息输出完整的最终回答，禁止描述思考过程。",
                    })
                    continue
                # 重试仍不合格的兜底：移除工具，无工具重写直到带够引用
                content = self._rewrite_until_cited(content, messages, final_check)
                self.conversation.append({"role": "assistant", "content": content})
                return content

            content = _strip_head_noise(content)
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
        return self._run_ask(DEEP_DIVE_PROMPT.format(question=question), question)

    def _orientation_snippet(self, limit: int = 1800) -> str:
        """紧凑的仓库方位（目录/入口/依赖），供规划器精确指定步骤；不消耗工具预算。"""
        from .tools.file_explorer import list_directory_structure
        from .tools.code_analyzer import analyze_dependencies, find_entry_points

        sections = [
            ("目录（深度 2）", lambda: list_directory_structure(self.repo_path, max_depth=2), 700),
            ("入口", lambda: find_entry_points(self.repo_path), 500),
            ("依赖", lambda: analyze_dependencies(self.repo_path), 500),
        ]
        parts: list[str] = []
        used = 0
        for label, fn, cap in sections:
            try:
                text = (fn() or "").strip()
            except Exception:
                text = ""
            if not text:
                continue
            text = text[:cap]
            if used + len(text) > limit:
                text = text[: max(0, limit - used)]
            used += len(text)
            parts.append(f"【{label}】\n{text}")
            if used >= limit:
                break
        return "\n\n".join(parts)

    def _ask_plan(self, question: str) -> list[str]:
        """Ask 前置规划：一次免工具调用拆解问题为 ≤5 步计划，存入 last_plan。"""
        orientation = self._orientation_snippet()
        messages = [
            {"role": "system", "content": ASK_PLAN_SYS},
            {
                "role": "user",
                "content": ASK_PLAN_USER.format(
                    orientation=orientation or "（未能提取到目录信息，第 1 步先用 search_code 按关键词定位）",
                    question=question,
                ),
            },
        ]
        plan: list[str] = []
        try:
            response = self._complete(messages, temperature=0.2, max_tokens=400)
            plan = _parse_ask_plan(response.choices[0].message.content or "")
        except Exception:
            plan = []
        if not plan:
            plan = [
                "search_code 按问题关键词定位相关文件 —— 找到实现所在",
                "read_file 读取核心实现并记录真实 file:line",
                "get_imports / get_function_signatures 理清调用链",
                "输出带 ≥2 处 file:line 引用的最终回答",
            ]
        self.last_plan = plan
        return plan

    def _run_ask(self, user_message: str, question: str) -> str:
        """Ask 主流程：先拆解计划，再按计划执行工具循环（含 Ask 工具预算）。"""
        plan = self._ask_plan(question)
        if plan:
            user_message = (
                f"{user_message}\n\n# 你的调研计划（按步执行；每步最多 1-2 个工具；"
                "工具总预算 20 次）\n"
                + "\n".join(f"{i + 1}. {step}" for i, step in enumerate(plan))
                + "\n\n# 收尾纪律\n"
                "- 每个子问题有真实 file:line 依据后即可进入下一步；不要为凑长回答重复读代码。\n"
                "- 收集到 ≥2 处可引用位置就输出最终回答（控制在 400-700 字），不要通读无关文件。\n"
                "- 直接输出回答正文，禁止复述任何系统提示。"
            )
        return self._run(user_message, final_check=_ask_final_check, tool_budget=ASK_TOOL_BUDGET)

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

    def get_architecture_diagram(self) -> str:
        """生成「总体架构图」：静态事实 + 一次 LLM 输出 Mermaid，不合格自动重写一次。

        图数据先来自代码事实（模块 / 入口 / README），LLM 只做语义分层与主链路标注，
        避免凭空画错拓扑；返回值仅含 Mermaid 代码，由前端负责渲染与降级。
        """
        from .diagram import architecture_facts, extract_mermaid_block, looks_valid_mermaid

        context = architecture_facts(self.repo_path)
        user = ARCH_DIAGRAM_PROMPT.format(repo_name=Path(self.repo_path).name, context=context)
        system = "你是代码库架构图解师。只输出 ```mermaid 代码块，禁止输出任何其他文字或解释。"

        content = self.direct(system, user, temperature=0.2, max_tokens=4096)
        code = extract_mermaid_block(content) or content.strip()
        ok, why = looks_valid_mermaid(code)
        if not ok:
            retry = (
                f"你上次输出的 Mermaid 不合格：{why}。"
                "请严格按原要求重试：只输出一个合法的 ```mermaid 代码块，禁止任何其他文字。\n\n"
                f"{user}"
            )
            content = self.direct(system, retry, temperature=0.2, max_tokens=4096)
            code = extract_mermaid_block(content) or content.strip()
            ok, why = looks_valid_mermaid(code)
            if not ok:
                raise ValueError(f"模型多次输出不符合 Mermaid 结构：{why}")
        return code

    def chat(self, message: str) -> str:
        """自由对话（Ask）：先拆解计划再执行，减少无方向探索。"""
        return self._run_ask(message, message)

    def reset_conversation(self) -> None:
        self.conversation = []
        self.last_tool_calls = []
        self.last_plan = []

    def get_last_tool_calls(self) -> list[dict]:
        return self.last_tool_calls

    def get_last_plan(self) -> list[str]:
        return self.last_plan
