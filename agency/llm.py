"""The team's brain: one entry point to Claude for every staff member.

Backends (config ``llm.backend``: ``auto`` | ``api`` | ``claude_cli`` | ``mock``, default auto):

- ``api``: the official Anthropic SDK (messages.create, adaptive effort via ``output_config``,
  structured JSON via ``output_config.format``, a manual tool-use loop and the server-side
  refusal fallback beta). Needs ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` (paid).
- ``claude_cli``: the Claude Code the owner already has, on his own plan (no extra cost); see
  :mod:`agency.llm_cli`. A per-day call cap (``llm.cli_daily_calls``) rests it until tomorrow.
- ``mock``: deterministic offline outputs, no network (tests, or nothing else available).

``auto`` order: ``AGENCY_MOCK_LLM=1`` → mock; an API key → api; a usable ``claude`` → claude_cli;
else mock. ``AGENCY_MOCK_LLM=1`` always wins. Each real call is logged (backend, purpose,
duration, ok) without any prompt content.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Settings

log = logging.getLogger("agency.llm")

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
_RATE_LIMIT_RETRIES = 3
BACKENDS = ("auto", "api", "claude_cli", "mock")
DEFAULT_CLI_DAILY_CALLS = 60
CLI_CALLS_KV = "llm.cli_calls"
REST_EVENT_HE = "המוח נח להיום — עד מחר הצוות כותב טקסטים מוכנים מראש"
BRAIN_ACTOR = "brain"
# Labels for the settings screen (advanced section).
BACKEND_LABEL_HE = {"claude_cli": "Claude (המנוי שלך)", "api": "מפתח API", "mock": "מצב דמו"}
BACKEND_LABEL_EN = {"claude_cli": "Claude Code (your plan)", "api": "Anthropic API key",
                    "mock": "demo mode (canned text)"}

_MAX_SNAPSHOT_TOOL_CHARS = 4000
_MAX_SNAPSHOT_CHARS = 20000
CLI_TOOLS_NOTE = (
    "\n\nIn this conversation you have no tools. The message ends with a snapshot of the real "
    "data your tools returned a moment ago - answer only from it and never invent numbers. "
    "You cannot change anything from here (move a post, create a promotion request): if Dor "
    "asks for a change, say in one line what you would change and that he can do it on the "
    "clip's card on the home screen (\"שנה משהו\" / \"פרטים\").")
_JSON_RETRY_NOTE = ("\n\nIMPORTANT: your previous answer did not match the required JSON "
                    "structure. Reply with the JSON object only, with every required field.")


class LLMError(RuntimeError):
    """The LLM call failed (API error, connection error, unparsable output)."""


class LLMRefusal(LLMError):
    """The model declined the request (``stop_reason == "refusal"``)."""


class _DailyCapReached(LLMError):
    """No ``claude`` process was started: today's ``llm.cli_daily_calls`` is used up (the
    public calls answer with canned text instead)."""


@dataclass
class Tool:
    """A client-side tool the model may call. ``fn`` receives the parsed input dict.

    ``snapshot_inputs`` is for the claude_cli backend, which cannot call tools: it runs the
    tools first and hands Claude the results. ``None`` = run once with ``{}`` when the tool
    has no required input; ``[]`` = never run it that way (tools that change something or
    are slow); a list of inputs = run once per input.
    """

    name: str
    description: str
    input_schema: dict = field(default_factory=lambda: {"type": "object", "properties": {}})
    fn: Callable[[dict], Any] = lambda _input: ""
    snapshot_inputs: list[dict] | None = None

    def to_api(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.input_schema}

    def default_inputs(self) -> list[dict]:
        """Inputs the claude_cli backend runs this tool with (see ``snapshot_inputs``)."""
        if self.snapshot_inputs is not None:
            return [dict(i) for i in self.snapshot_inputs]
        return [] if (self.input_schema or {}).get("required") else [{}]


def _mock_forced() -> bool:
    return os.environ.get("AGENCY_MOCK_LLM", "").strip().lower() in ("1", "true", "yes", "on")


def _has_api_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _is_mock_env() -> bool:
    """Legacy check (API backend only): mock forced or no API key in the environment."""
    return _mock_forced() or not _has_api_key()


def configured_backend(settings: Settings) -> str:
    """``llm.backend`` from config (unknown values mean ``auto``)."""
    choice = str(settings.get("llm.backend", "auto") or "auto").strip().lower()
    return choice if choice in BACKENDS else "auto"


def select_backend(settings: Settings) -> tuple[str, Any]:
    """(backend, cli_info): ``"api"`` | ``"claude_cli"`` | ``"mock"``. Never a paid call."""
    if _mock_forced():
        return "mock", None
    choice = configured_backend(settings)
    if choice == "mock":
        return "mock", None
    if choice in ("auto", "api") and _has_api_key():
        return "api", None
    if choice == "api":
        log.warning("llm.backend is 'api' but there is no API key: demo mode")
        return "mock", None
    from .llm_cli import find_claude
    info = find_claude(settings)
    if info is not None:
        return "claude_cli", info
    if choice == "claude_cli":
        log.warning("llm.backend is 'claude_cli' but Claude Code was not found: demo mode")
    return "mock", None


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


def _api_content(user: str | list) -> str | list:
    """User content for the API: private keys of blocks (``_path``) removed."""
    if not isinstance(user, list):
        return user
    return [{k: v for k, v in b.items() if not str(k).startswith("_")} if isinstance(b, dict)
            else b for b in user]


def _cli_content(user: str | list) -> tuple[str, list[Any]]:
    """(prompt text, images) for the CLI: text blocks joined; image blocks become their
    frame file (``_path``) or decoded bytes."""
    if not isinstance(user, list):
        return str(user), []
    texts: list[str] = []
    images: list[Any] = []
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
              "image/webp": ".webp"}
    for block in user:
        if isinstance(block, str):
            texts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        elif isinstance(block, dict) and block.get("type") == "image":
            path = block.get("_path")
            if path and Path(path).is_file():
                images.append(Path(path))
                continue
            src = block.get("source") or {}
            if src.get("type") == "base64" and src.get("data"):
                try:
                    images.append((base64.standard_b64decode(src["data"]),
                                   suffix.get(str(src.get("media_type")), ".jpg")))
                except (ValueError, TypeError):
                    continue
    return "\n\n".join(t for t in texts if t), images


def _caller_purpose() -> str:
    """``<staff>.<method>`` of the staff member calling the LLM (for the call log)."""
    frame = sys._getframe(2)
    for _ in range(8):
        if frame is None:
            break
        obj = frame.f_locals.get("self")
        if obj is not None and not isinstance(obj, LLM):
            name = getattr(obj, "name", None)
            if isinstance(name, str) and name:
                return f"{name}.{frame.f_code.co_name}"
        frame = frame.f_back
    return "other"


class LLM:
    """Single entry point to Claude for the whole agency."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.model: str = settings.get("llm.model", DEFAULT_MODEL) or DEFAULT_MODEL
        self.effort: str = settings.get("llm.effort", "medium") or "medium"
        self.max_tokens: int = int(settings.get("llm.max_tokens", 16000) or 16000)
        self.use_fallbacks: bool = bool(settings.get("llm.fallbacks", True))
        self._client: Any = None
        self._backend: str | None = None  # resolved lazily (detection runs `claude --version`)
        self._cli: Any = None
        self._cli_info: Any = None
        self._lock = threading.Lock()
        self._db_obj: Any = None

    # ------------------------------------------------------------------ backend
    @property
    def backend(self) -> str:
        """``"api"``, ``"claude_cli"`` or ``"mock"`` (detected once, on first use)."""
        if self._backend is None:
            with self._lock:
                if self._backend is None:
                    backend, info = select_backend(self.settings)
                    if backend == "claude_cli":
                        from .llm_cli import DEFAULT_TIMEOUT_S, ClaudeCLI
                        timeout = _number(self.settings.get("llm.cli_timeout_s"),
                                          DEFAULT_TIMEOUT_S)
                        self._cli_info = info
                        self._cli = ClaudeCLI(info, model=self.model, effort=self.effort,
                                              timeout_s=timeout, on_call=self._count_cli_call)
                    self._backend = backend
        return self._backend

    @backend.setter
    def backend(self, value: str) -> None:
        if value not in ("api", "claude_cli", "mock"):
            raise ValueError(f"unknown backend {value!r}")
        if value == "claude_cli" and self._cli is None:
            raise ValueError("claude_cli backend needs a detected Claude Code")
        self._backend = value

    @property
    def mock(self) -> bool:
        """True when the answers are canned: mock backend, or the CLI resting for today."""
        backend = self.backend
        return backend == "mock" or (backend == "claude_cli" and self.resting())

    @mock.setter
    def mock(self, value: bool) -> None:
        if value:
            self._backend = "mock"
        elif self._backend == "mock":
            self._backend = None  # re-detect

    @property
    def cli(self) -> Any:
        """The :class:`agency.llm_cli.ClaudeCLI` runner (claude_cli backend only)."""
        return self._cli if self.backend == "claude_cli" else None

    def brain_info(self) -> dict:
        """What thinks for the team, for the settings screen / ``agency brain`` (no secrets)."""
        backend = self.backend
        info: dict[str, Any] = {"backend": backend, "configured": configured_backend(self.settings),
                                "label_he": BACKEND_LABEL_HE[backend],
                                "label_en": BACKEND_LABEL_EN[backend], "resting": False}
        if backend == "claude_cli":
            usage = self._cli_usage()
            info.update(resting=self.resting(), calls_today=usage["calls"],
                        daily_cap=self.daily_cap(), version=self._cli_info.version)
        return info

    # ------------------------------------------------------------------ daily cap (claude_cli)
    def daily_cap(self) -> int:
        return int(_number(self.settings.get("llm.cli_daily_calls"), DEFAULT_CLI_DAILY_CALLS))

    def _db(self) -> Any:
        if self._db_obj is None:
            from .db import DB
            self._db_obj = DB(self.settings.db_path)
        return self._db_obj

    def _today(self) -> str:
        name = self.settings.get("publishing.timezone", "Asia/Jerusalem") or "Asia/Jerusalem"
        try:
            from zoneinfo import ZoneInfo
            tz: Any = ZoneInfo(name)
        except Exception:
            tz = timezone(timedelta(hours=3))
        return datetime.now(timezone.utc).astimezone(tz).date().isoformat()

    @staticmethod
    def _usage_of(data: Any, today: str) -> dict:
        """Today's ``{day, calls, rested}`` from the stored kv value (a new day starts at 0)."""
        if not isinstance(data, dict) or data.get("day") != today:
            return {"day": today, "calls": 0, "rested": False}
        try:
            calls = int(data.get("calls") or 0)
        except (TypeError, ValueError):
            calls = 0
        return {"day": today, "calls": calls, "rested": bool(data.get("rested"))}

    def _cli_usage(self) -> dict:
        try:
            data = self._db().kv_get(CLI_CALLS_KV) or {}
        except Exception:
            data = {}
        return self._usage_of(data, self._today())

    def resting(self) -> bool:
        """True when today's CLI call cap is used up (canned answers until tomorrow)."""
        return self._cli_usage()["calls"] >= self.daily_cap()

    _usage_lock = threading.Lock()

    def _count_cli_call(self) -> None:
        """Called by the runner right before each real ``claude`` process starts: reserves one
        of today's calls, atomically across threads and processes (``BEGIN IMMEDIATE``).

        Raises :class:`_DailyCapReached` (and nothing starts) when the cap is used up, so
        retries and concurrent callers can never go past it. A DB failure lets the call run
        (the cap is a safety net, not a reason to stop the team)."""
        from .db import DB

        cap, today = self.daily_cap(), self._today()
        try:
            with LLM._usage_lock, self._db().immediate() as conn:
                usage = self._usage_of(DB.kv_get_in(conn, CLI_CALLS_KV), today)
                if usage["calls"] >= cap:
                    raise _DailyCapReached("today's Claude call cap is used up")
                usage["calls"] += 1
                DB.kv_set_in(conn, CLI_CALLS_KV, usage)
        except _DailyCapReached:
            raise
        except Exception:
            log.warning("could not store the Claude call counter")

    def _rest_notice(self) -> None:
        """Once per day: tell the activity feed that the brain rests until tomorrow."""
        from .db import DB

        today = self._today()
        try:
            with LLM._usage_lock, self._db().immediate() as conn:
                usage = self._usage_of(DB.kv_get_in(conn, CLI_CALLS_KV), today)
                if usage["rested"]:
                    return
                usage["rested"] = True
                DB.kv_set_in(conn, CLI_CALLS_KV, usage)
            from .events import emit  # after the commit: emit writes through its own connection
            emit(self._db(), BRAIN_ACTOR, REST_EVENT_HE, "warning")
        except Exception:
            log.warning("could not record the brain's rest")

    def _active(self) -> str:
        """The backend for THIS call: the CLI rests (mock) once today's cap is used up."""
        backend = self.backend
        if backend == "claude_cli" and self.resting():
            self._rest_notice()
            return "mock"
        return backend

    # ------------------------------------------------------------------ call log
    def _timed(self, backend: str, purpose: str, fn: Callable[[], Any]) -> Any:
        """Run one real call; log backend, purpose, duration and ok (never prompt content)."""
        start = time.monotonic()
        status = "failed"
        try:
            out = fn()
            status = "ok"
            return out
        except _DailyCapReached:
            status = "rested"
            raise
        finally:
            took = time.monotonic() - start
            log.info("llm call backend=%s purpose=%s duration=%.1fs status=%s",
                     backend, purpose, took, status)
            try:
                from .events import emit
                emit(self._db(), BRAIN_ACTOR, f"[{backend}] {purpose}: {took:.1f}s, {status}",
                     "warning" if status == "failed" else "debug")
            except Exception:  # the feed must never break a call
                pass

    def _cli_or_rest(self, purpose: str, fn: Callable[[], Any], canned: Callable[[], Any]) -> Any:
        """One claude_cli call; when the daily cap stops it before a process starts (a retry
        or a concurrent caller used the last slot), the canned answer + the rest notice."""
        try:
            return self._timed("claude_cli", purpose, fn)
        except _DailyCapReached:
            self._rest_notice()
            return canned()

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
        return [{"role": "user", "content": _api_content(user)}]

    # ------------------------------------------------------------------ public API
    def complete(self, system: str, user: str | list, *, max_tokens: int | None = None,
                 purpose: str | None = None) -> str:
        """Plain text completion."""
        backend = self._active()
        if backend == "mock":
            return self._mock_text(system, user)
        purpose = purpose or _caller_purpose()
        if backend == "claude_cli":
            prompt, images = _cli_content(user)
            return self._cli_or_rest(purpose, lambda: str(
                self._cli.run(system, prompt, images=images).get("result") or "").strip(),
                lambda: self._mock_text(system, user))

        def api() -> str:
            response = self._create(system=system, messages=self._user_messages(user),
                                    max_tokens=max_tokens or self.max_tokens,
                                    output_config=self._output_config())
            self._check_stop(response)
            return _text_of(response.content)
        return self._timed(backend, purpose, api)

    def complete_json(self, system: str, user: str | list, schema: dict, *,
                      mock_value: dict, purpose: str | None = None) -> dict:
        """Structured JSON output constrained by ``schema``. Mock mode returns ``mock_value``."""
        backend = self._active()
        if backend == "mock":
            return copy.deepcopy(mock_value)
        purpose = purpose or _caller_purpose()
        if backend == "claude_cli":
            return self._cli_or_rest(purpose, lambda: self._cli_json(system, user, schema),
                                     lambda: copy.deepcopy(mock_value))

        def api() -> dict:
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
        return self._timed(backend, purpose, api)

    def _cli_json(self, system: str, user: str | list, schema: dict) -> dict:
        """CLI structured output: ``structured_output``, else the first JSON object of the
        text; required keys checked; one retry; then a plain LLMError."""
        from .llm_cli import REASON_TURNS, CLIFailure, extract_json

        prompt, images = _cli_content(user)
        strict = _strict_schema(schema)
        required = [k for k in (schema.get("required") or []) if isinstance(k, str)]
        for attempt in range(2):
            try:
                data = self._cli.run(system, prompt if attempt == 0 else prompt + _JSON_RETRY_NOTE,
                                     schema=strict, images=images)
            except CLIFailure as exc:
                if attempt == 0 and exc.reason == REASON_TURNS:
                    continue
                raise
            obj = data.get("structured_output")
            if not isinstance(obj, dict):
                obj = extract_json(str(data.get("result") or ""))
            if isinstance(obj, dict) and all(k in obj for k in required):
                return obj
            log.warning("Claude answer did not match the JSON structure (attempt %s)", attempt + 1)
        raise LLMError("Claude החזיר תשובה במבנה לא נכון")

    def run_tools(self, system: str, user: str, tools: list[Tool], *, max_turns: int = 12,
                  mock_text: str = "", purpose: str | None = None) -> str:
        """Manual agentic loop: let Claude call ``tools`` until it finishes; return final text.

        Mock mode returns ``mock_text`` without calling any tool. The claude_cli backend runs
        the tools first (:meth:`tool_snapshot`) and makes ONE call without tools.
        """
        backend = self._active()
        if backend == "mock":
            return mock_text or self._mock_text(system, user)
        purpose = purpose or _caller_purpose()
        if backend == "claude_cli":
            def cli() -> str:
                snapshot = json.dumps(self.tool_snapshot(tools), ensure_ascii=False, default=str)
                prompt = (f"{user}\n\nREAL DATA SNAPSHOT (your tools' results, fetched just "
                          "now; ids are internal - never show them):\n" + snapshot)
                return str(self._cli.run(system + CLI_TOOLS_NOTE, prompt).get("result")
                           or "").strip()
            return self._cli_or_rest(purpose, cli,
                                     lambda: mock_text or self._mock_text(system, user))
        return self._timed(backend, purpose,
                           lambda: self._api_tools(system, user, tools, max_turns))

    @staticmethod
    def tool_snapshot(tools: list[Tool]) -> dict:
        """Run every tool with its default inputs (see :meth:`Tool.default_inputs`) and return
        a compact ``{name: result}`` map. A failing tool reports its error type only."""
        out: dict[str, Any] = {}
        total = 0
        for tool in tools:
            for inp in tool.default_inputs():
                key = tool.name if not inp else \
                    f"{tool.name}({', '.join(f'{k}={v}' for k, v in inp.items())})"
                try:
                    result = tool.fn(dict(inp))
                except Exception as exc:
                    log.warning("snapshot tool %s failed: %s", tool.name, type(exc).__name__)
                    result = {"error": type(exc).__name__}
                text = result if isinstance(result, str) else \
                    json.dumps(result, ensure_ascii=False, default=str)
                if len(text) > _MAX_SNAPSHOT_TOOL_CHARS:
                    text = text[:_MAX_SNAPSHOT_TOOL_CHARS] + "…(truncated)"
                if total + len(text) > _MAX_SNAPSHOT_CHARS:
                    out[key] = "(left out: snapshot is full)"
                    continue
                total += len(text)
                try:
                    out[key] = json.loads(text)
                except ValueError:
                    out[key] = text
        return out

    def _api_tools(self, system: str, user: str, tools: list[Tool], max_turns: int) -> str:
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
        """Base64 image content block (jpeg/png/gif/webp) for vision requests.

        The private ``_path`` key (removed before an API call) lets the claude_cli backend
        hand Claude the frame file itself."""
        path = Path(path)
        media = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                 ".gif": "image/gif", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
        data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data},
                "_path": str(path.resolve())}

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


def _number(value: Any, default: float) -> float:
    """A non-negative number from config, else ``default``."""
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number and number >= 0 else default
