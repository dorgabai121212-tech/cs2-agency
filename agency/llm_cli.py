"""The "claude_cli" brain: the team thinks with the Claude Code the owner already has.

The owner has no paid API key ($0 budget) but Claude Code is installed and logged in on his
PC (his old studio ran its AI jobs the same way). This module runs ``claude -p`` (print mode)
as a child process:

- list args only (never a shell), the prompt on stdin (UTF-8), one JSON result on stdout;
- the child's working folder is a fresh private temp folder (deleted afterwards), so no project
  CLAUDE.md, settings, hooks or memory of any folder of ours is picked up;
- **his plan, not a key**: the child's environment loses every ``ANTHROPIC_*`` variable (a paid
  key or bearer token, extra headers, another endpoint), the cloud-provider switches
  (``CLAUDE_CODE_USE_BEDROCK`` / ``_VERTEX`` / ``_FOUNDRY``), every agency secret and anything
  named like a secret. A long-lived token from ``claude setup-token`` that
  ``secret("CLAUDE_CODE_OAUTH_TOKEN")`` resolves (env, our keyring, or read-only the studio's
  ``claude_token``) goes into the child's environment only, never on the command line;
- the system prompt goes in a file in that temp folder (``--system-prompt-file``): no 32k
  Windows command-line limit and nothing for cmd.exe to re-parse;
- tools are off (``--tools ""``). A vision call gets ``Read`` only, inside the temp folder that
  holds copies of the frames (reads outside it are denied by Claude Code in print mode), and
  ``--max-turns`` caps every call;
- ``--bare`` is NOT used: it makes Claude Code accept only ``ANTHROPIC_API_KEY`` and never read
  the owner's login (verified: "Authentication error" without a key). ``--safe-mode`` (when this
  Claude Code has it) + ``--setting-sources project`` in the empty temp folder +
  ``--strict-mcp-config`` give the same isolation (no hooks, plugins, MCP, CLAUDE.md, skills);
- an npm ``claude.cmd`` shim is run as the ``node cli.js`` it would start (no cmd.exe), and a
  timeout stops the whole process tree, not only the direct child.

Detection never makes a paid call: ``claude --version``, ``claude --help`` and one
``claude -p --max-turns 1 --system-prompt-file <missing file>`` with stdin closed, which stops
at the missing file before any model call (15 s timeout each; cached). That last check exists
because Claude Code hides ``--max-turns`` and ``--system-prompt-file`` from ``--help`` (2.1.x):
an unsupported flag answers "unknown option" instead. Error messages are short and plain (no
environment, no tokens, no raw output).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import STUDIO_ALIASES, Settings, secret, secret_source
from .llm import LLMError, LLMRefusal

log = logging.getLogger("agency.llm.cli")

ENV_SWITCH = "AGENCY_CLAUDE_CLI"  # "off" = never use the CLI (tests); a path = use exactly it
TOKEN_NAME = "CLAUDE_CODE_OAUTH_TOKEN"
PROBE_TIMEOUT_S = 15
DEFAULT_TIMEOUT_S = 240
DENIED_TOOLS = ("Bash", "Edit", "Write", "WebFetch", "WebSearch", "NotebookEdit", "Agent")
# Flags Claude Code must list in --help for us to use it at all.
REQUIRED_FLAGS = ("--output-format", "--tools")
# Flags Claude Code accepts but hides from --help (2.1.x): detected by a free -p check.
HIDDEN_FLAGS = ("--max-turns", "--system-prompt-file")
_NEGATIVE_TTL_S = 300.0
_CMD_SAFE_ARG = re.compile(r"^[\w.,:\\/ +=@~-]*$")  # args we dare hand to a .cmd launcher
_VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")
_FLAG_RE = re.compile(r"(?<![\w-])--[a-zA-Z][\w-]*")
_UNKNOWN_OPTION_RE = re.compile(r"unknown option\W*(--[a-zA-Z][\w-]*)", re.I)
_CREATE_NO_WINDOW = 0x08000000
_MAX_ARG_PROMPT_CHARS = 20000  # inline --system-prompt only (Windows command lines end at 32k)
_KILL_TIMEOUT_S = 15
_DRAIN_TIMEOUT_S = 5
_CMD_SUFFIXES = (".cmd", ".bat")


# =========================================================================== detection
@dataclass(frozen=True)
class CLIInfo:
    """A usable Claude Code: what to start, its version and the long flags it accepts.

    ``script`` is set for an npm install without a native ``claude.exe``: then ``path`` is
    ``node`` and the call is ``node <cli.js> ...`` (what the ``claude.cmd`` shim would run).
    """

    path: str
    version: str
    flags: frozenset[str]
    script: str = ""

    def supports(self, flag: str) -> bool:
        return flag in self.flags

    @property
    def argv(self) -> list[str]:
        """The command that starts Claude Code (before any of our arguments)."""
        return [self.path, self.script] if self.script else [self.path]

    @property
    def shown_path(self) -> str:
        """The path to show the owner (``cli.js`` rather than ``node``)."""
        return self.script or self.path

    @property
    def is_cmd(self) -> bool:
        """A Windows ``.cmd``/``.bat`` launcher (cmd.exe re-parses its arguments)."""
        return not self.script and Path(self.path).suffix.lower() in _CMD_SUFFIXES


_cache_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, CLIInfo | None]] = {}


def clear_cache() -> None:
    """Forget detection results (tests; after installing Claude Code)."""
    with _cache_lock:
        _cache.clear()


def cli_switch() -> str:
    return os.environ.get(ENV_SWITCH, "").strip()


def cli_disabled() -> bool:
    """``AGENCY_CLAUDE_CLI=off`` turns the CLI brain off (the test suite does this)."""
    return cli_switch().lower() in ("off", "0", "false", "no", "none")


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW) if os.name == "nt" else 0


def _npm_package(shim: Path) -> Path:
    return shim.parent / "node_modules" / "@anthropic-ai" / "claude-code"


def _native_for(path: Path) -> Path:
    """For an npm ``claude.cmd`` shim, the native ``claude.exe`` it starts (when present)."""
    if path.suffix.lower() in _CMD_SUFFIXES:
        exe = _npm_package(path) / "bin" / "claude.exe"
        try:
            if exe.is_file():
                return exe
        except OSError:
            pass
    return path


def _npm_node_launch(shim: Path) -> tuple[str, str] | None:
    """For an npm ``claude.cmd`` shim without ``claude.exe``: the ``(node, cli.js)`` it would
    start, found the way the shim finds them (``node.exe`` next to it, else ``node`` on PATH).
    Running these directly keeps cmd.exe out: no argument re-parsing, and a timeout can stop
    Claude Code itself instead of only the cmd.exe in front of it."""
    try:
        script = _npm_package(shim) / "cli.js"
        if not script.is_file():
            return None
        local = shim.parent / "node.exe"
        node = str(local) if local.is_file() else shutil.which("node")
    except OSError:
        return None
    return (node, str(script)) if node else None


def candidate_paths(settings: Settings | None) -> list[Path]:
    """Where to look for Claude Code, best first (a forced env path is the only candidate)."""
    forced = cli_switch()
    if forced and not cli_disabled():
        return [Path(forced).expanduser()]
    out: list[Path] = []
    configured = str((settings.get("llm.claude_path", "") if settings else "") or "").strip()
    if configured:
        out.append(Path(configured).expanduser())
    names = ("claude.exe", "claude", "claude.cmd") if os.name == "nt" else ("claude",)
    found = [shutil.which(n) for n in names]
    home = Path.home()
    if os.name == "nt":
        out += [Path(p) for p in found[:1] if p]
        appdata = os.environ.get("APPDATA")
        if appdata:
            npm = Path(appdata) / "npm"
            out.append(npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
        out.append(home / ".local" / "bin" / "claude.exe")
        out += [Path(p) for p in found[1:] if p]
        if appdata:
            out.append(Path(appdata) / "npm" / "claude.cmd")
    else:
        out += [Path(p) for p in found if p]
    out += [home / ".local" / "bin" / "claude", home / ".claude" / "local" / "claude"]
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        p = _native_for(p)
        key = os.path.normcase(str(p))
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


# =========================================================================== processes
def _kill_tree(proc: subprocess.Popen, *, windows: bool | None = None) -> None:
    """Stop ``proc`` and every process it started (best effort; never raises).

    A launcher (npm's ``claude.cmd`` = cmd.exe -> node.exe) runs Claude Code as a grandchild
    that holds our output pipes, so killing only the direct child leaves it running.
    Windows: ``taskkill /T /F`` (list args, no shell). POSIX: the child leads its own process
    group (``start_new_session``), and the whole group is killed.
    """
    if windows is None:
        windows = os.name == "nt"
    if windows:
        root =os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows"
        try:
            subprocess.run([os.path.join(root, "System32", "taskkill.exe"), "/T", "/F",
                            "/PID", str(proc.pid)], check=False, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=_KILL_TIMEOUT_S, creationflags=_creationflags())
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (OSError, AttributeError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _drain(proc: subprocess.Popen) -> None:
    """After a kill: collect what is left for a few seconds, then give up on the pipes."""
    try:
        proc.communicate(timeout=_DRAIN_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        # Something we could not stop still holds the pipes. On Windows the reader threads
        # own them (closing them here would block); on POSIX close our ends.
        if os.name != "nt":
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass


def run_process(argv: list[str], *, cwd: str, env: dict[str, str], timeout: float,
                input: str | None = None) -> subprocess.CompletedProcess:
    """Run ``argv`` (list args, never a shell) with UTF-8 text pipes and ``input`` on stdin
    (stdin closed when None). On timeout (or any interruption) the whole process tree is
    stopped before this returns, then ``subprocess.TimeoutExpired`` is raised.
    Raises OSError / ValueError when the program cannot start."""
    extra: dict[str, Any] = {} if os.name == "nt" else {"start_new_session": True}
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL if input is None else subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", cwd=cwd, env=env,
                            creationflags=_creationflags(), **extra)
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except BaseException:  # TimeoutExpired, KeyboardInterrupt, ...: never leave it running
        _kill_tree(proc)
        _drain(proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


# =========================================================================== probing
def _run_probe(launch: list[str], args: list[str],
               cwd: str | None = None) -> subprocess.CompletedProcess | None:
    tmp = cwd or tempfile.mkdtemp(prefix="agency-claude-probe-")
    try:
        return run_process(launch + args, cwd=tmp, env=child_env(None), timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    finally:
        if cwd is None:
            shutil.rmtree(tmp, ignore_errors=True)


def _hidden_flags(launch: list[str], listed: frozenset[str], *, cmd: bool) -> frozenset[str]:
    """Which of :data:`HIDDEN_FLAGS` this Claude Code accepts, though ``--help`` hides them.

    Free: ``claude -p --max-turns 1 --system-prompt-file <missing file>`` with stdin closed
    stops at the missing file ("System prompt file not found") before any model call; every
    check carries that stopper. Claude Code (commander) reports the FIRST option it does not
    know ("unknown option '<flag>'"), so the flags before it were understood; when that is
    ``--max-turns``, the check repeats for the file flag alone. Anything unclear (no answer,
    a timeout, another message) means "not known".
    """
    order = list(HIDDEN_FLAGS)  # --max-turns first: the stopper must come last
    known = {f for f in order if f in listed}
    wanted = [f for f in order if f not in listed]
    tmp = tempfile.mkdtemp(prefix="agency-claude-probe-")
    try:
        missing = str(Path(tmp) / "no-such-system-prompt.txt")
        while wanted:
            stopper = "--system-prompt-file" in wanted or "--system-prompt-file" in known
            if not stopper:
                break  # never run -p without the missing-file stop
            args = ["-p"]
            if "--no-session-persistence" in listed:
                args.append("--no-session-persistence")
            if "--max-turns" in wanted:
                args += ["--max-turns", "1"]
            args += ["--system-prompt-file", missing]
            if cmd and not all(_CMD_SAFE_ARG.match(a) for a in args):
                break
            proc = _run_probe(launch, args, cwd=tmp)
            if proc is None:
                break
            unknown = _UNKNOWN_OPTION_RE.search(f"{proc.stderr or ''}\n{proc.stdout or ''}")
            if unknown is None:
                known.update(wanted)
                break
            flag = unknown.group(1)
            if flag not in wanted:
                break  # an option we did not ask about: we cannot tell
            known.update(wanted[:wanted.index(flag)])  # understood before the unknown one
            wanted = wanted[wanted.index(flag) + 1:]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return frozenset(known)


def probe(path: Path) -> CLIInfo | None:
    """``claude --version``, ``claude --help`` and the hidden-flag check (all local and free).
    None when this Claude Code is not usable."""
    path = Path(path)
    try:
        if not path.is_file():
            return None
    except OSError:
        return None
    launch, script = [str(path)], ""
    if path.suffix.lower() in _CMD_SUFFIXES:
        node_launch = _npm_node_launch(path)
        if node_launch is not None:
            launch, script = list(node_launch), node_launch[1]
    ver = _run_probe(launch, ["--version"])
    if ver is None or ver.returncode != 0:
        return None
    match = _VERSION_RE.search(ver.stdout or "")
    if not match:
        return None
    helped = _run_probe(launch, ["--help"])
    if helped is None or helped.returncode != 0:
        return None
    flags = frozenset(_FLAG_RE.findall(helped.stdout or ""))
    if not all(f in flags for f in REQUIRED_FLAGS):
        log.info("Claude Code %s at %s is too old for the team (missing flags)",
                 match.group(1), path)
        return None
    is_cmd = not script and path.suffix.lower() in _CMD_SUFFIXES
    flags |= _hidden_flags(launch, flags, cmd=is_cmd)
    if is_cmd and "--system-prompt-file" not in flags:
        # cmd.exe would re-parse an inline system prompt: every call would be refused, so
        # this one is not usable (the next candidate, or demo mode, is honest instead)
        log.info("Claude Code %s at %s cannot take a system prompt file through cmd.exe",
                 match.group(1), path)
        return None
    return CLIInfo(path=launch[0], version=match.group(1), flags=flags, script=script)


def find_claude(settings: Settings | None) -> CLIInfo | None:
    """The first usable Claude Code (cached per process; misses are re-checked after 5 min)."""
    if cli_disabled():
        return None
    configured = str((settings.get("llm.claude_path", "") if settings else "") or "")
    key = (cli_switch(), configured)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit is not None:
        when, info = hit
        if info is not None and Path(info.path).is_file():
            return info
        if info is None and now - when < _NEGATIVE_TTL_S:
            return None
    info = None
    for path in candidate_paths(settings):
        info = probe(path)
        if info is not None:
            break
    with _cache_lock:
        _cache[key] = (now, info)
    if info is not None:
        log.info("team brain: Claude Code %s (%s)", info.version, info.path)
    return info


# =========================================================================== environment
# Claude Code bills (or authenticates as) whatever these name instead of the owner's login: a
# paid key or bearer token (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN - ranked above the login and
# CLAUDE_CODE_OAUTH_TOKEN), extra headers or another endpoint (ANTHROPIC_CUSTOM_HEADERS,
# ANTHROPIC_BASE_URL), a cloud provider (CLAUDE_CODE_USE_BEDROCK / _VERTEX / _FOUNDRY).
_WITHHELD_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_")
_WITHHELD_NAMES = frozenset({"AWS_BEARER_TOKEN_BEDROCK"})
# Anything named like a secret (GITHUB_TOKEN, X_API_SECRET, REDDIT_PASSWORD...): the child has
# no tools that could need one.
_SECRET_NAME_RE = re.compile(
    r"(?:^|_)(?:API_?KEYS?|TOKENS?|SECRETS?|PASSWORDS?|PASSWD|CREDENTIALS?)(?:_|$)")


def _agency_secret_names() -> set[str]:
    """Every secret name the agency knows: the .env names, each adapter's required and
    optional secrets and the studio aliases (CLAUDE_CODE_OAUTH_TOKEN is handled apart)."""
    names = {n.upper() for n in STUDIO_ALIASES}
    try:
        from .platforms import registry
        from .settings_store import allowed_env_names

        names.update(n.upper() for n in allowed_env_names())
        classes: list[Any] = []
        for key in registry.ALL_KEYS:
            try:
                classes.append(registry.adapter_class(key))
            except Exception:  # a broken adapter module must not break the brain
                continue
        try:
            classes.append(registry.zernio_class())
        except Exception:
            pass
        for cls in classes:
            for name in getattr(cls, "optional_secrets", []) or []:
                if isinstance(name, str):
                    names.add(name.upper())
    except Exception:  # pragma: no cover - the registry must never break the brain
        pass
    return names


def withheld(name: str, agency_names: set[str] | None = None) -> bool:
    """True when the ``claude`` child must not get environment variable ``name``."""
    upper = name.upper()
    if upper == TOKEN_NAME or upper in _WITHHELD_NAMES or upper.startswith(_WITHHELD_PREFIXES):
        return True
    if _SECRET_NAME_RE.search(upper):
        return True
    return upper in (agency_names if agency_names is not None else _agency_secret_names())


def child_env(token: str | None) -> dict[str, str]:
    """The child's environment: ours minus every paid credential, provider switch and secret
    (see :func:`withheld`), plus the owner's login token when we have one."""
    agency_names = _agency_secret_names()
    env = {k: v for k, v in os.environ.items() if not withheld(k, agency_names)}
    if token:
        env[TOKEN_NAME] = token
    return env


# =========================================================================== output parsing
def parse_output(stdout: str) -> dict | None:
    """The result object of ``--output-format json`` (warnings may come before it).

    Takes the last line that parses as an object with ``"type": "result"`` or ``"subtype"``
    (or the last such element of a JSON array, as older versions print), else the whole text.
    """
    text = stdout or ""

    def pick(obj: Any) -> dict | None:
        if isinstance(obj, dict) and (obj.get("type") == "result" or "subtype" in obj):
            return obj
        if isinstance(obj, list):
            for item in reversed(obj):
                if isinstance(item, dict) and item.get("type") == "result":
                    return item
        return None

    for line in reversed([ln.strip() for ln in text.splitlines()]):
        if not line or line[0] not in "{[":
            continue
        try:
            found = pick(json.loads(line))
        except ValueError:
            continue
        if found is not None:
            return found
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start >= 0:
        try:
            return pick(json.loads(text[start:]))
        except ValueError:
            return None
    return None


def extract_json(text: str) -> dict | None:
    """The first JSON object in a free-text answer (code fences and chatter allowed)."""
    text = str(text or "")
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            obj, _end = decoder.raw_decode(text, match.start())
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


_AUTH_RE = re.compile(r"auth|log ?in|logged|401|invalid api key|oauth|credential", re.I)
_LIMIT_RE = re.compile(r"usage limit|limit reached|rate.?limit|429|overloaded|529|quota", re.I)
_MODEL_RE = re.compile(r"model", re.I)
_MODEL_BAD_RE = re.compile(r"not found|not available|unavailable|invalid|404|does not exist|"
                           r"not supported|no access", re.I)

REASON_AUTH = "auth"
REASON_LIMIT = "limit"
REASON_MODEL = "model"
REASON_TURNS = "turns"
REASON_OTHER = "other"

_REASON_TEXT = {
    REASON_AUTH: "Claude לא מחובר במחשב — צריך להתחבר מחדש ב-Claude Code",
    REASON_LIMIT: "Claude עמוס או שהמכסה של המנוי נגמרה — ננסה שוב אחר כך",
    REASON_MODEL: "המודל שנבחר לא זמין במנוי של Claude",
    REASON_TURNS: "Claude לא הספיק לסיים את התשובה",
    REASON_OTHER: "Claude לא הצליח לענות",
}


class CLIFailure(LLMError):
    """The CLI call failed; ``reason`` is one of the REASON_* codes (message is plain text)."""

    def __init__(self, reason: str, detail: str = ""):
        text = _REASON_TEXT.get(reason, _REASON_TEXT[REASON_OTHER])
        super().__init__(f"{text} ({detail})" if detail else text)
        self.reason = reason


def classify(data: dict) -> str:
    """REASON_* code for a failed result object (looks only at short status text)."""
    subtype = str(data.get("subtype") or "")
    if "max_turns" in subtype or "max_structured" in subtype:
        return REASON_TURNS
    text = " ".join(str(data.get(k) or "") for k in ("result", "error", "api_error_status"))[:400]
    if _AUTH_RE.search(text):
        return REASON_AUTH
    if _LIMIT_RE.search(text):
        return REASON_LIMIT
    if _MODEL_RE.search(text) and _MODEL_BAD_RE.search(text):
        return REASON_MODEL
    return REASON_OTHER


# =========================================================================== runner
class ClaudeCLI:
    """Runs one ``claude -p`` call per request. Thread-safe (no shared mutable state)."""

    def __init__(self, info: CLIInfo, *, model: str = "", effort: str = "",
                 timeout_s: float = DEFAULT_TIMEOUT_S,
                 on_call: Callable[[], None] | None = None):
        self.info = info
        self.model = (model or "").strip()
        self.effort = (effort or "").strip()
        self.timeout_s = float(timeout_s or DEFAULT_TIMEOUT_S)
        self.on_call = on_call  # counts each real process start (daily cap)
        self._skip_token = False  # an injected token was refused once: use the login instead
        self._skip_model = False  # the configured model is not in the plan: use the default

    # ---------------------------------------------------------------- arguments
    def build_args(self, system_arg: list[str], *, schema: dict | None, read_dir: Path | None,
                   max_turns: int) -> list[str]:
        """The full argv (without the prompt, which goes to stdin)."""
        info = self.info
        args = info.argv + ["-p", "--output-format", "json"]
        for flag in ("--no-session-persistence", "--strict-mcp-config", "--safe-mode"):
            if info.supports(flag):
                args.append(flag)
        if info.supports("--setting-sources"):
            args += ["--setting-sources", "project"]  # the empty temp folder: nothing to load
        if self.model and not self._skip_model and info.supports("--model"):
            args += ["--model", self.model]
        if self.effort in ("low", "medium", "high", "xhigh", "max") and info.supports("--effort"):
            args += ["--effort", self.effort]
        args += system_arg
        if read_dir is not None:
            args += ["--tools", "Read", "--add-dir", str(read_dir)]
        else:
            args += ["--tools", ""]
        if info.supports("--disallowedTools"):
            args += ["--disallowedTools", ",".join(DENIED_TOOLS)]
        if info.supports("--max-turns"):
            args += ["--max-turns", str(max_turns)]
        if schema is not None and not info.is_cmd and info.supports("--json-schema"):
            args += ["--json-schema", json.dumps(schema, ensure_ascii=False,
                                                 separators=(",", ":"))]
        return args

    def _system_arg(self, system: str, schema: dict | None, tmp: Path) -> list[str]:
        """``--system-prompt-file`` with the text in the private temp folder: no command-line
        length limit (Windows ends at 32,767 chars) and nothing for cmd.exe to re-parse
        (quotes, ``%``, ``&``). Only a Claude Code without that flag gets the short text
        inline. Without ``--json-schema`` the schema goes into the system prompt."""
        if schema is not None and (self.info.is_cmd or not self.info.supports("--json-schema")):
            system = (f"{system}\n\nReply with ONLY one JSON object (no prose, no code fence) "
                      "that matches this JSON Schema:\n"
                      f"{json.dumps(schema, ensure_ascii=False)}")
        if self.info.supports("--system-prompt-file"):
            path = tmp / "system.txt"
            path.write_text(system, encoding="utf-8")
            return ["--system-prompt-file", str(path)]
        if self.info.is_cmd or len(system) > _MAX_ARG_PROMPT_CHARS:
            raise CLIFailure(REASON_OTHER, "Claude Code ישן מדי")
        return ["--system-prompt", system]

    # ---------------------------------------------------------------- run
    def run(self, system: str, prompt: str, *, schema: dict | None = None,
            images: Iterable[Any] = (), max_turns: int | None = None) -> dict:
        """One call; returns the CLI's result object (``result``, ``structured_output``...).

        ``images`` are frame files (``Path``) or ``(bytes, suffix)`` pairs; they are copied
        into the private temp folder and Claude reads them with the Read tool.
        Raises :class:`CLIFailure` / :class:`LLMRefusal` with a plain message.
        """
        images = list(images)
        tmp = Path(tempfile.mkdtemp(prefix="agency-claude-"))
        try:
            read_dir: Path | None = None
            if images:
                read_dir = tmp / "frames"
                read_dir.mkdir()
                paths = self._copy_images(images, read_dir)
                prompt = (f"{prompt}\n\nThe frames are image files. Open each one with the Read "
                          "tool, in this order, before you answer:\n"
                          + "\n".join(f"{i}. {p}" for i, p in enumerate(paths, 1)))
            turns = max_turns or ((3 if schema is not None else 2)
                                  + (len(images) + 2 if images else 0))
            system_arg = self._system_arg(system, schema, tmp)
            data = self._invoke(system_arg, prompt, schema=schema, read_dir=read_dir,
                                max_turns=turns, cwd=tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return data

    @staticmethod
    def _copy_images(images: list[Any], folder: Path) -> list[Path]:
        out: list[Path] = []
        for i, item in enumerate(images, 1):
            if isinstance(item, tuple):
                data, suffix = item
                dst = folder / f"frame_{i:02d}{suffix or '.jpg'}"
                dst.write_bytes(data)
            else:
                src = Path(item)
                dst = folder / f"frame_{i:02d}{src.suffix.lower() or '.jpg'}"
                shutil.copyfile(src, dst)
            out.append(dst.resolve())
        return out

    def _token(self) -> tuple[str | None, str | None]:
        if self._skip_token:
            return None, None
        try:
            return secret(TOKEN_NAME), secret_source(TOKEN_NAME)
        except Exception:  # pragma: no cover - keyring trouble means "no token"
            return None, None

    def _invoke(self, system_arg: list[str], prompt: str, *, schema: dict | None,
                read_dir: Path | None, max_turns: int, cwd: Path) -> dict:
        """Start the process (with at most one retry for a refused token / model)."""
        for attempt in range(2):
            token, source = self._token()
            args = self.build_args(system_arg, schema=schema, read_dir=read_dir,
                                   max_turns=max_turns)
            if self.info.is_cmd and not all(_CMD_SAFE_ARG.match(a)
                                            for a in args[len(self.info.argv):]):
                raise CLIFailure(REASON_OTHER, "נתיב או הגדרה עם תווים מיוחדים")
            try:
                return self._once(args, prompt, token, cwd)
            except CLIFailure as exc:
                if attempt == 0 and exc.reason == REASON_AUTH and token and source != "env":
                    log.warning("the saved Claude token was refused; using the computer's login")
                    self._skip_token = True
                    continue
                if attempt == 0 and exc.reason == REASON_MODEL and self.model \
                        and not self._skip_model:
                    log.warning("model %s is not available on this plan; using the default",
                                self.model)
                    self._skip_model = True
                    continue
                raise
        raise CLIFailure(REASON_OTHER)  # pragma: no cover

    def _once(self, args: list[str], prompt: str, token: str | None, cwd: Path) -> dict:
        if self.on_call is not None:
            self.on_call()
        try:
            proc = run_process(args, input=prompt, timeout=self.timeout_s, cwd=str(cwd),
                               env=child_env(token))
        except subprocess.TimeoutExpired:
            raise CLIFailure(REASON_OTHER, f"לא ענה תוך {self.timeout_s:.0f} שניות") from None
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise CLIFailure(REASON_OTHER, f"לא נפתח: {type(exc).__name__}") from None
        data = parse_output(proc.stdout)
        if data is None:
            if proc.returncode != 0:
                raise CLIFailure(REASON_OTHER, f"קוד {proc.returncode}")
            raise CLIFailure(REASON_OTHER, "תשובה לא מובנת")
        if str(data.get("stop_reason") or "") == "refusal":
            raise LLMRefusal("Claude סירב לבקשה")
        if data.get("is_error") or str(data.get("subtype") or "success") != "success" \
                or proc.returncode != 0:
            raise CLIFailure(classify(data))
        return data


__all__ = ["CLIFailure", "CLIInfo", "ClaudeCLI", "DENIED_TOOLS", "ENV_SWITCH", "HIDDEN_FLAGS",
           "TOKEN_NAME", "candidate_paths", "child_env", "classify", "clear_cache",
           "cli_disabled", "extract_json", "find_claude", "parse_output", "probe", "run_process",
           "withheld"]
