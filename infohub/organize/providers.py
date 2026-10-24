"""AI providers for the Layer 2 organizer. Each returns a Reply or raises ProviderError.

- "openai_compatible": any OpenAI-style /chat/completions endpoint (DeepSeek by default); stdlib HTTP only.
- "anthropic": Claude through the official SDK (pip install -r requirements-ai.txt).
API keys are read from the environment variable named in `ai.api_key_env`; never stored in the repo.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import prompt

FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}


@dataclass
class Reply:
    text: str
    stop: str                                     # "end" | "max_tokens" | "refusal"
    usage: dict = field(default_factory=dict)     # input, output, cache_read, cache_write


class ProviderError(RuntimeError):
    def __init__(self, msg: str, retryable: bool = False, fatal: bool = False):
        super().__init__(msg)
        self.retryable, self.fatal = retryable, fatal


def api_key(ai: dict) -> str | None:
    name = ai.get("api_key_env") or ""
    if not name:
        return None
    value = os.environ.get(name)
    if not value and os.name == "nt":  # processes started before the user variable was set
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                value = winreg.QueryValueEx(k, name)[0]
        except OSError:
            value = None
    return value or None


class OpenAICompatible:
    """POST {base_url}/chat/completions with JSON-object output. The schema is part of the system prompt."""

    def __init__(self, ai: dict):
        self.ai = ai
        self.key = api_key(ai)
        if not self.key:
            raise ProviderError(f"environment variable {ai.get('api_key_env')} is not set", fatal=True)
        self.url = ai["base_url"].rstrip("/") + "/chat/completions"

    def complete(self, payload: dict, system: str | None = None, schema: dict | None = None) -> Reply:
        system = (system or prompt.SYSTEM) + "\nReply with one JSON object only, matching this JSON Schema:\n" \
                 + json.dumps(schema or prompt.SCHEMA, ensure_ascii=False)
        body = {"model": self.ai["model"], "max_tokens": int(self.ai.get("max_tokens", 8000)),
                "response_format": {"type": "json_object"}, "stream": False,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]}
        req = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(req, timeout=float(self.ai.get("timeout_seconds", 180))) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            code = exc.code
            if code in (401, 403):
                raise ProviderError(f"HTTP {code}: check {self.ai.get('api_key_env')}", fatal=True) from None
            if code == 402:
                raise ProviderError("HTTP 402: insufficient balance", fatal=True) from None
            if code == 404:
                raise ProviderError(f"HTTP 404: model or endpoint not found ({self.ai['model']})", fatal=True) from None
            raise ProviderError(f"HTTP {code}", retryable=code == 429 or code >= 500) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise ProviderError(f"connection: {type(exc).__name__}", retryable=True) from None
        choice = (data.get("choices") or [{}])[0]
        u = data.get("usage") or {}
        cache_hit = u.get("prompt_cache_hit_tokens") or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
        finish = choice.get("finish_reason")
        return Reply(text=(choice.get("message") or {}).get("content") or "",
                     stop={"length": "max_tokens", "content_filter": "refusal"}.get(finish, "end"),
                     usage={"input": max(0, (u.get("prompt_tokens") or 0) - cache_hit), "output": u.get("completion_tokens") or 0,
                            "cache_read": cache_hit, "cache_write": 0})


class Anthropic:
    """Claude Messages API with JSON-schema structured output and a cached system prompt."""

    def __init__(self, ai: dict, client=None):
        self.ai = ai
        if client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ProviderError("the 'anthropic' package is not installed (pip install -r requirements-ai.txt)", fatal=True) from exc
            client = anthropic.Anthropic(max_retries=2, timeout=120.0)
        self.client = client

    def complete(self, payload: dict, system: str | None = None, schema: dict | None = None) -> Reply:
        import anthropic
        model = self.ai["model"]
        kwargs = dict(model=model, max_tokens=16000,
                      system=[{"type": "text", "text": system or prompt.SYSTEM, "cache_control": {"type": "ephemeral"}}],
                      output_config={"effort": self.ai.get("effort", "low"),
                                     "format": {"type": "json_schema", "schema": schema or prompt.SCHEMA}},
                      messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}])
        try:
            if model in FALLBACK_MODELS:
                resp = self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = self.client.messages.create(**kwargs)
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise ProviderError(f"{type(exc).__name__}: check ANTHROPIC_API_KEY", fatal=True) from exc
        except anthropic.NotFoundError as exc:
            raise ProviderError(f"model not found: {model}", fatal=True) from exc
        except anthropic.BadRequestError as exc:
            raise ProviderError(f"BadRequest: {str(exc)[:200]}") from exc
        except anthropic.RateLimitError as exc:
            raise ProviderError("rate limited", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"HTTP {exc.status_code}", retryable=exc.status_code >= 500) from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError("connection error", retryable=True) from exc
        u = resp.usage
        return Reply(text=next((b.text for b in resp.content if b.type == "text"), ""),
                     stop={"refusal": "refusal", "max_tokens": "max_tokens"}.get(resp.stop_reason, "end"),
                     usage={"input": getattr(u, "input_tokens", 0) or 0, "output": getattr(u, "output_tokens", 0) or 0,
                            "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
                            "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0})


def make(ai: dict, client=None):
    kind = ai.get("provider", "openai_compatible")
    if kind == "anthropic":
        return Anthropic(ai, client)
    if kind == "openai_compatible":
        return OpenAICompatible(ai)
    raise ProviderError(f"unknown ai.provider: {kind}", fatal=True)
