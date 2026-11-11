from __future__ import annotations

import httpx

from .anthropic import AnthropicProvider
from .base import ModelInfo, Provider, RateInfo, UpstreamStream, validate_base_url
from .gemini import GeminiProvider
from .openai_compat import OpenAICompatProvider

KINDS: dict[str, type[Provider]] = {
    "openai": OpenAICompatProvider,
    "gemini": GeminiProvider,
    "anthropic": AnthropicProvider,
}

# Seeded on `apigate init`. Providers without keys are inert.
DEFAULT_PROVIDERS = [
    ("openai", "openai", "https://api.openai.com/v1"),
    ("anthropic", "anthropic", "https://api.anthropic.com/v1"),
    ("gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("deepseek", "openai", "https://api.deepseek.com/v1"),
    ("groq", "openai", "https://api.groq.com/openai/v1"),
    ("mistral", "openai", "https://api.mistral.ai/v1"),
    ("xai", "openai", "https://api.x.ai/v1"),
    ("openrouter", "openai", "https://openrouter.ai/api/v1"),
    ("together", "openai", "https://api.together.xyz/v1"),
]


def build(kind: str, name: str, base_url: str, client: httpx.AsyncClient, extra: dict | None = None) -> Provider:
    return KINDS[kind](name, base_url, client, extra)


__all__ = ["KINDS", "DEFAULT_PROVIDERS", "build", "Provider", "ModelInfo", "RateInfo", "UpstreamStream", "validate_base_url"]
