from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from .config import InferenceConfig, ProviderKind, load_inference_config


@dataclass(frozen=True, slots=True)
class InferenceResult:
    provider: str
    model: str
    content: str
    used_network: bool
    raw: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "content": self.content,
            "used_network": self.used_network,
            "raw": self.raw,
        }


class InferenceError(RuntimeError):
    pass


class OpenAICompatibleInferenceClient:
    """Small OpenAI-compatible client for Fireworks and vLLM.

    We avoid SDK lock-in for the hackathon slice. Fireworks and vLLM both expose
    `/v1/chat/completions`, so stdlib HTTP is enough for the first working gateway.
    """

    def __init__(self, config: InferenceConfig | None = None) -> None:
        self._config = config or load_inference_config()

    @property
    def config(self) -> InferenceConfig:
        return self._config

    def complete(
        self,
        *,
        agent: str,
        system: str,
        user: str,
        temperature: float = 0.1,
        max_tokens: int = 400,
    ) -> InferenceResult:
        model = self._config.model_for_agent(agent)
        if self._config.provider is ProviderKind.OFFLINE:
            return InferenceResult(
                provider=self._config.provider.value,
                model=model,
                content="offline: deterministic ShelfWise cascade is active",
                used_network=False,
            )
        if not self._config.api_key_present:
            raise InferenceError("LLM_API_KEY is required when LLM_BASE_URL is configured")

        payload = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        url = self._config.chat_completions_url()
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise InferenceError("Inference provider request failed") from exc

        content = _extract_content(raw)
        return InferenceResult(
            provider=self._config.provider.value,
            model=model,
            content=content,
            used_network=True,
            raw=raw,
        )


def _extract_content(raw: dict[str, Any]) -> str:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise InferenceError("Inference response missing choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise InferenceError("Inference response missing message")
    content = message.get("content")
    if not isinstance(content, str):
        raise InferenceError("Inference response missing text content")
    return content
