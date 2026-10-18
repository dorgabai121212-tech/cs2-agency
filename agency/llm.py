"""Claude API wrapper used by every staff member.

Real mode follows the official Anthropic SDK usage (messages.create, adaptive effort via
``output_config``, structured JSON via ``output_config.format``, a manual tool-use loop and the
server-side refusal fallback beta). Mock mode (no API key, or ``AGENCY_MOCK_LLM=1``) returns
deterministic outputs and never touches the network, so the whole agency works offline in tests.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import Settings

log = logging.getLogger("agency.llm")

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_RATE_LIMIT_RETRIES = 3


class LLMError(RuntimeError):
    """The LLM call failed (API error, connection error, unparsable output)."""


class LLMRefusal(LLMError):
    """The model declined the request (``stop_reason == "refusal"``)."""


@dataclass
class Tool:
    """A client-side tool the model may call. ``fn`` receives the parsed input dict."""

    name: str
    description: str
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    fn: Callable[[dict], str] = lambda _input: ""

    def to_api(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}


def _is_mock_env() -> bool:
    if os.environ.get("AGENCY_MOCK_LLM", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _strict_schema(schema: dict) -> dict:
    """Copy of ``schema`` where every object without ``additionalProperties`` gets ``false``
    (required by structured outputs)."""
    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: walk(v) for k, v in node.items()}
            if node.get("type") == "object" and "additionalProperties" not in node:
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(copy.deepcopy(schema))


def _text_of(content: Any) -> str:
    """Concatenate text blocks of a response's content list."""
    parts = [getattr(b, "text", "") for b in content or [] if getattr(b, "type", None) == "text"]
    return "\n".join(p for p in parts if p).strip()


def _short(value: Any, n: int = 120) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text[:n] + ("…" if len(text) > n else "")


class LLM:
    """Single entry point to Claude for the whole agency."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model: str = settings.get("llm.model", DEFAULT_MODEL) or DEFAULT_MODEL
        self.effort: str = settings.get("llm.effort", "medium") or "medium"
        self.max_tokens: int = int(settings.get("llm.max_tokens", 16000) or 16000)
        self.use_fallbacks: bool = bool(settings.get("llm.fallbacks", True))
        self.mock: bool = _is_mock_env()
        self._client: Any = None

    # ------------------------------------------------------------------ client plumbing
    @property
    def client(self) -> Any:
        """Lazily-created ``anthropic.Anthropic`` client (credentials from env)."""
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic()
        return self._client

    def _create(self, **kwargs: Any) -> Any:
        """One messages.create call with refusal fallback and the SDK error-handling chain."""
        import anthropic

        kwargs.setdefault("model", self.model)
        kwargs.setdefault("max_tokens", self.max_tokens)
        for attempt in range(_RATE_LIMIT_RETRIES + 1):
            try:
                if self.use_fallbacks:
                    try:
                        return self.client.beta.messages.create(
                            betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
                    except anthropic.BadRequestError as exc:
                        log.warning("server-side fallback rejected (%s); retrying without it",
                                    getattr(exc, "status_code", 400))
                        self.use_fallbacks = False
                return self.client.messages.create(**kwargs)
            except anthropic.RateLimitError as exc:
                if attempt >= _RATE_LIMIT_RETRIES:
                    raise LLMError(f"rate limited by Claude API: {exc.message}") from exc
                wait = 2 ** (attempt + 1)
                log.warning("rate limited; retrying in %ss", wait)
                time.sleep(wait)
            except anthropic.APIStatusError as exc:
                raise LLMError(f"Claude API error {exc.status_code}: {exc.message}") from exc
            except anthropic.APIConnectionError as exc:
                raise LLMError(f"could not reach Claude API: {exc}") from exc
        raise LLMError("unreachable")  # pragma: no cover

    def _output_config(self, extra: dict | None = None) -> dict:
        cfg: dict[str, Any] = {"effort": self.effort}
        if extra:
            cfg.update(extra)
        return cfg

    @staticmethod
    def _check_stop(response: Any) -> str:
        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            raise LLMRefusal("Claude declined this request")
        if stop == "max_tokens":
            log.warning("response truncated at max_tokens")
        return stop or ""

    @staticmethod
    def _user_messages(user: str | list) -> list[dict]:
        return [{"role": "user", "content": user}]

    # ------------------------------------------------------------------ public API
    def complete(self, system: str, user: str | list, *, max_tokens: int | None = None) -> str:
        """Plain text completion."""
        if self.mock:
            return self._mock_text(system, user)
        response = self._create(system=system, messages=self._user_messages(user),
                                max_tokens=max_tokens or self.max_tokens,
                                output_config=self._output_config())
        self._check_stop(response)
        return _text_of(response.content)

    def complete_json(self, system: str, user: str | list, schema: dict, *,
                      mock_value: dict) -> dict:
        """Structured JSON output constrained by ``schema``. Mock mode returns ``mock_value``."""
        if self.mock:
            return copy.deepcopy(mock_value)
        response = self._create(
            system=system, messages=self._user_messages(user),
            output_config=self._output_config(
                {"format": {"type": "json_schema", "schema": _strict_schema(schema)}}))
        stop = self._check_stop(response)
        text = _text_of(response.content)
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise LLMError(f"model returned invalid JSON (stop_reason={stop})") from exc
        if not isinstance(data, dict):
            raise LLMError("model returned JSON that is not an object")
        return data

    def run_tools(self, system: str, user: str, tools: list[Tool], *, max_turns: int = 12,
                  mock_text: str = "") -> str:
        """Manual agentic loop: let Claude call ``tools`` until it finishes; return final text.

        Mock mode returns ``mock_text`` without calling any tool.
        """
        if self.mock:
            return mock_text or self._mock_text(system, user)
        by_name = {t.name: t for t in tools}
        api_tools = [t.to_api() for t in tools]
        messages: list[dict] = self._user_messages(user)
        last_text = ""
        for _turn in range(max_turns):
            response = self._create(system=system, messages=messages, tools=api_tools,
                                    output_config=self._output_config())
            stop = self._check_stop(response)
            text = _text_of(response.content)
            last_text = text or last_text
            # Always keep the full assistant content (tool_use blocks, thinking, etc.).
            messages.append({"role": "assistant", "content": response.content})

            if stop == "tool_use":
                results = [self._run_tool(by_name, block) for block in response.content
                           if getattr(block, "type", None) == "tool_use"]
                if not results:
                    return last_text
                messages.append({"role": "user", "content": results})  # ONE user message
                continue
            if stop == "pause_turn":
                continue  # server paused a long turn; resend so it can resume
            # end_turn, stop_sequence, max_tokens or anything else: we are done.
            return last_text
        log.warning("run_tools hit max_turns=%s", max_turns)
        return last_text

    @staticmethod
    def _run_tool(by_name: dict[str, Tool], block: Any) -> dict:
        name = getattr(block, "name", "")
        tool_input = getattr(block, "input", None) or {}
        tool = by_name.get(name)
        result: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id}
        if tool is None:
            result.update(content=f"Unknown tool: {name}", is_error=True)
            return result
        try:
            output = tool.fn(dict(tool_input))
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False, default=str)
            result["content"] = output
        except Exception as exc:  # report to the model instead of crashing the loop
            log.warning("tool %s failed: %s", name, exc)
            result.update(content=f"Error: {type(exc).__name__}: {exc}", is_error=True)
        return result

    @staticmethod
    def image_block(path: Path) -> dict:
        """Base64 image content block (jpeg/png/gif/webp) for vision requests."""
        path = Path(path)
        media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".gif": "image/gif", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
        data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}

    # ------------------------------------------------------------------ mock helpers
    @staticmethod
    def _mock_text(system: str, user: str | list) -> str:
        """Deterministic offline answer derived from the input."""
        if isinstance(user, list):
            texts = [b.get("text", "") for b in user if isinstance(b, dict) and b.get("type") == "text"]
            user_text = " ".join(texts)
        else:
            user_text = str(user)
        digest = hashlib.sha1(f"{system}\n{user_text}".encode("utf-8")).hexdigest()[:8]
        return f"[מצב הדגמה / mock {digest}] {_short(user_text)}"
