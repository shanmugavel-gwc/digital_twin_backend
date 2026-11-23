"""LLM behind an interface. DomoTextLLM wraps the Domo AI Text Generation API as a LangChain LLM:
POST https://{subdomain}.domo.com/api/ai/v1/text/generation  header X-DOMO-Developer-Token
body {input, temperature, maxTokens, model?, system?}  ->  response field `output`.
The endpoint has NO native tool calling; the JSON action loop lives in agent.py. Swap providers by passing any object with
`invoke(prompt, system=...) -> str`. The token is never logged or included in error messages."""
from __future__ import annotations

import time
from typing import Any, Optional, Protocol

import httpx
from langchain_core.language_models.llms import LLM
from pydantic import SecretStr


class LLMError(RuntimeError):
    pass


class TextLLM(Protocol):
    def invoke(self, prompt: str, **kwargs: Any) -> str: ...


class DomoTextLLM(LLM):
    subdomain: str
    token: SecretStr
    model: str = ""
    temperature: float = 0.1
    max_tokens: int = 1500
    timeout: float = 90.0
    retries: int = 2

    @property
    def _llm_type(self) -> str:
        return "domo-text-generation"

    def _call(self, prompt: str, stop: Optional[list[str]] = None, run_manager: Any = None, **kwargs: Any) -> str:
        body: dict[str, Any] = {"input": prompt, "temperature": self.temperature, "maxTokens": self.max_tokens}
        if self.model:
            body["model"] = self.model
        if kwargs.get("system"):
            body["system"] = kwargs["system"]
        url = f"https://{self.subdomain}.domo.com/api/ai/v1/text/generation"
        headers = {"X-DOMO-Developer-Token": self.token.get_secret_value(), "Content-Type": "application/json"}
        last = "unknown error"
        for i in range(self.retries + 1):
            try:
                r = httpx.post(url, headers=headers, json=body, timeout=self.timeout)
                if r.status_code == 200:
                    data = r.json()
                    out = data.get("output")
                    if out is None and data.get("choices"):
                        out = data["choices"][0].get("output")
                    if not isinstance(out, str):
                        raise LLMError("Domo response had no text output")
                    return out
                last = f"HTTP {r.status_code}"
                if r.status_code in (401, 403):
                    raise LLMError(f"Domo rejected the credentials ({last}); check DOMO_TOKEN")
                if r.status_code not in (429, 500, 502, 503, 504):
                    raise LLMError(f"Domo request failed ({last})")
            except httpx.HTTPError as e:
                last = type(e).__name__
            time.sleep(1.5 * (i + 1))
        raise LLMError(f"Domo AI unavailable after retries ({last})")


def default_llm() -> DomoTextLLM:
    from .config import settings
    tok = settings.domo_token.get_secret_value()
    if not tok:
        raise LLMError("DOMO_TOKEN is not configured")
    return DomoTextLLM(subdomain=settings.domo_subdomain, token=settings.domo_token, model=settings.domo_model)
