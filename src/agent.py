"""Agent 主循环：OpenAI 兼容 API + 工具调用（不依赖 LangChain/LangGraph）。

设计要点（面试可讲）：
- 自己实现 ReAct 式循环：模型决定调用哪个工具 → 执行 → 结果回传 → 直到模型给出最终回答
- 确定性工具负责"读代码"，LLM 只负责"总结与推理"，降低幻觉
- 每次工具调用都被记录，供界面展示"Agent 做了什么"
"""

from __future__ import annotations

import json
from pathlib import Path

from openai import OpenAI

from .config import LLMConfig
from .prompts import SYSTEM_PROMPT, OVERVIEW_PROMPT, DEEP_DIVE_PROMPT
from .tools import call_tool, get_tool_schemas

MAX_TOOL_ROUNDS = 15


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

    def _system_message(self) -> dict:
        return {"role": "system", "content": SYSTEM_PROMPT.format(repo_path=self.repo_path)}

    def _run(self, user_message: str) -> str:
        self.conversation.append({"role": "user", "content": user_message})
        messages = [self._system_message(), *self.conversation]
        self.last_tool_calls = []

        for _ in range(self.max_tool_rounds):
            response = self.client.chat.completions.create(
                model=self.config.model,
                messages=messages,
                tools=get_tool_schemas(),
                temperature=0,
            )
            message = response.choices[0].message
            if message.tool_calls:
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
            self.conversation.append({"role": "assistant", "content": content})
            return content

        return "已达到最大工具调用轮数，问题较复杂，请拆分后重试。"

    def get_overview(self) -> str:
        """生成代码库学习概览。"""
        return self._run(OVERVIEW_PROMPT)

    def ask(self, question: str) -> str:
        """针对代码库提问。"""
        return self._run(DEEP_DIVE_PROMPT.format(question=question))

    def chat(self, message: str) -> str:
        """自由对话。"""
        return self._run(message)

    def reset_conversation(self) -> None:
        self.conversation = []
        self.last_tool_calls = []

    def get_last_tool_calls(self) -> list[dict]:
        return self.last_tool_calls