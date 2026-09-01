"""LLM 提供商配置：DeepSeek / OpenRouter / Groq / OpenAI / 自定义 OpenAI 兼容服务。"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env": "OPENROUTER_API_KEY",
        "model": "xiaomi/mimo-v2-flash:free",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env": "GROQ_API_KEY",
        "model": "llama-3.1-8b-instant",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env": "OPENAI_API_KEY",
        "model": "gpt-4o-mini",
    },
}


@dataclass
class LLMConfig:
    provider: str
    api_key: str
    base_url: str
    model: str


def resolve_config(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> LLMConfig:
    """解析最终 LLM 配置。

    优先级：显式参数 > 自定义服务环境变量（LLM_BASE_URL 等）> 各提供商默认环境变量。
    """
    custom_base = os.getenv("LLM_BASE_URL")
    if custom_base and not provider:
        return LLMConfig(
            provider="custom",
            api_key=api_key or os.getenv("LLM_API_KEY") or "EMPTY",
            base_url=custom_base,
            model=model or os.getenv("LLM_MODEL") or "qwen2.5-coder:7b",
        )

    provider = provider or "deepseek"
    if provider not in PROVIDERS:
        raise ValueError(f"未知提供商：{provider}，可选：{', '.join(PROVIDERS)}")

    info = PROVIDERS[provider]
    key = api_key or os.getenv(info["env"])
    if not key:
        raise ValueError(
            f"缺少 API Key：请设置环境变量 {info['env']}（或 --api-key 参数）。"
            f"\nDeepSeek 注册：https://platform.deepseek.com/  OpenRouter：https://openrouter.ai/keys"
        )
    return LLMConfig(
        provider=provider,
        api_key=key,
        base_url=info["base_url"],
        model=model or info["model"],
    )