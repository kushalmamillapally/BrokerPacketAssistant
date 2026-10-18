"""Minimal Ollama client using its native /api/chat endpoint with JSON-schema structured output."""
from __future__ import annotations

import json
import os
import time
from typing import Protocol

import httpx


class LLMError(Exception):
    """Any failure to obtain a usable model response. Never replaced by simulated output."""


class LLMClient(Protocol):
    model: str

    def chat_json(self, system: str, user: str, schema: dict) -> dict: ...


class OllamaClient:
    def __init__(self, base_url: str | None = None, model: str | None = None, timeout: float | None = None):
        self.base_url = (base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
        self.model = model or os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
        self.timeout = timeout or float(os.getenv("LLM_TIMEOUT_SECONDS", "240"))

    def chat_json(self, system: str, user: str, schema: dict) -> dict:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "format": schema,
            "stream": False,
            "options": {"temperature": 0, "num_ctx": int(os.getenv("LLM_NUM_CTX", "4096"))},
        }
        start = time.monotonic()
        try:
            resp = httpx.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
        except httpx.ConnectError as exc:
            raise LLMError(f"Cannot reach Ollama at {self.base_url} (is `ollama serve` running?)") from exc
        except httpx.TimeoutException as exc:
            raise LLMError(f"Model call timed out after {self.timeout:.0f}s") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Model request failed: {exc}") from exc

        if resp.status_code == 404:
            raise LLMError(f"Model '{self.model}' not found in Ollama; run `ollama pull {self.model}`")
        if resp.status_code != 200:
            raise LLMError(f"Ollama returned HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            content = resp.json()["message"]["content"]
            data = json.loads(content)
        except (KeyError, ValueError, TypeError) as exc:
            raise LLMError("Model returned output that is not valid JSON") from exc
        if not isinstance(data, dict):
            raise LLMError("Model returned JSON that is not an object")
        data["_elapsed_seconds"] = round(time.monotonic() - start, 1)
        return data

    def health(self) -> dict:
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=5)
            names = [m["name"] for m in resp.json().get("models", [])]
        except Exception:
            return {"reachable": False, "model": self.model, "model_available": False}
        available = any(n == self.model or n.split(":")[0] == self.model for n in names)
        return {"reachable": True, "model": self.model, "model_available": available}
