"""LLM 提供商配置：DeepSeek / OpenRouter / Groq / OpenAI / 自定义 OpenAI 兼容服务。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

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
        "model": "z-ai/glm-5.2:free",
        # 免费模型共享池偶发限流：主模型 429/过载时按顺序自动降级
        "fallback": [
            "minimax/minimax-m3:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
        ],
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
    fallback_models: list[str] = field(default_factory=list)


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

    provider = provider or "openrouter"
    if provider not in PROVIDERS:
        raise ValueError(f"未知提供商：{provider}，可选：{', '.join(PROVIDERS)}")

    info = PROVIDERS[provider]
    chosen_model = model or info["model"]
    fallback_models = [m for m in info.get("fallback", []) if m != chosen_model]
    key = api_key or os.getenv(info["env"])
    if not key:
        raise ValueError(
            f"缺少 API Key：请设置环境变量 {info['env']}（或 --api-key 参数）。"
            f"\nOpenRouter 注册：https://openrouter.ai/keys（默认提供商，免费模型 z-ai/glm-5.2:free）"
            f"\nDeepSeek 注册：https://platform.deepseek.com/"
        )
    return LLMConfig(
        provider=provider,
        api_key=key,
        base_url=info["base_url"],
        model=chosen_model,
        fallback_models=fallback_models,
    )
