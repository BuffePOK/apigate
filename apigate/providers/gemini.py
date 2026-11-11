"""Gemini through Google's OpenAI-compatible endpoint; model discovery via the native API
(which tells us generateContent vs embedContent and token limits)."""
from __future__ import annotations

from .base import ModelInfo, classify_model
from .openai_compat import OpenAICompatProvider


class GeminiProvider(OpenAICompatProvider):
    kind = "gemini"

    def _native_base(self) -> str:
        b = self.base_url
        if b.endswith("/openai"):
            b = b[: -len("/openai")]
        return b

    async def list_models(self, secret: str) -> list[ModelInfo]:
        out: dict[str, ModelInfo] = {}
        token = ""
        for _ in range(20):
            url = f"{self._native_base()}/models?pageSize=1000" + (f"&pageToken={token}" if token else "")
            # key travels in a header, never in the URL (URLs end up in logs)
            r = await self._request("GET", url, {"x-goog-api-key": secret, "Accept": "application/json"})
            j = r.json()
            for m in j.get("models", []):
                name = m.get("name", "")
                mid = name[7:] if name.startswith("models/") else name
                if not mid:
                    continue
                methods = set(m.get("supportedGenerationMethods") or [])
                kind = classify_model(mid)
                if "embedContent" in methods and "generateContent" not in methods:
                    kind = "embedding"
                elif methods and "generateContent" not in methods and kind == "chat":
                    kind = "other"
                out[mid] = ModelInfo(mid, kind, m.get("inputTokenLimit"))
            token = j.get("nextPageToken") or ""
            if not token:
                break
        return list(out.values())
