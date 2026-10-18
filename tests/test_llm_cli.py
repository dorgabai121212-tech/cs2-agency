"""The claude_cli brain (agency.llm_cli + agency.llm): offline, with a fake ``claude``.

The fake is a small Python script (shebang → this interpreter) that answers ``--version`` and
``--help`` like Claude Code 2.1.302 (its real ``--help`` text, which hides ``--max-turns`` and
``--system-prompt-file``), rejects flags an older version would not know ("unknown option"),
stops at a missing ``--system-prompt-file`` like the real one, records argv / stdin / the env
/ the frames it can see for every ``-p`` call, and prints canned output from a JSON script.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from agency import llm_cli
from agency.llm import LLM, LLMError, LLMRefusal, Tool

pytestmark = pytest.mark.skipif(os.name == "nt", reason="fake CLI uses a POSIX shebang")

# The real `claude --help` of Claude Code 2.1.302 (it does not list the hidden flags).
HELP = (Path(__file__).parent / "fixtures" / "claude_help_2.1.302.txt").read_text(encoding="utf-8")

FAKE = r'''#!{python}
import json, os, sys, time
args = sys.argv[1:]
probe_log = os.environ.get("FAKE_CLAUDE_PROBES")

def probe(tag, out="", err="", code=0):
    if probe_log:
        with open(probe_log, "a", encoding="utf-8") as fh:
            fh.write(tag + "\n")
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.exit(code)

if args[:1] in (["--version"], ["--help"]):
    if args[0] == "--version":
        probe("--version", "2.1.302 (Claude Code)\n")
    probe("--help", open(os.environ["FAKE_CLAUDE_HELP"], encoding="utf-8").read())
unknown_file = os.environ.get("FAKE_CLAUDE_UNKNOWN")
unknown = json.load(open(unknown_file, encoding="utf-8")) if unknown_file else []
for a in args:  # like commander: options are checked before anything runs
    if a in unknown:
        probe("unknown " + a, err="error: unknown option '%s'\n" % a, code=1)
sysfile = None
if "--system-prompt-file" in args:
    path = args[args.index("--system-prompt-file") + 1]
    if not os.path.isfile(path):
        probe("-p check", err="Error: System prompt file not found: %s\n" % path, code=1)
    sysfile = open(path, encoding="utf-8").read()
stdin = sys.stdin.read()
frames = []
if "--add-dir" in args:
    folder = args[args.index("--add-dir") + 1]
    frames = sorted(os.path.join(folder, n) for n in os.listdir(folder))
keys = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "ZERNIO_API_KEY", "META_ACCESS_TOKEN",
        "ANTHROPIC_AUTH_TOKEN", "AGENCY_TEST_HARMLESS")
record = {{"argv": args, "stdin": stdin, "cwd": os.getcwd(), "frames": frames, "sysfile": sysfile,
          "frames_exist": all(os.path.isfile(f) for f in frames),
          "env": {{k: os.environ.get(k) for k in keys}}, "env_names": sorted(os.environ)}}
log = os.environ["FAKE_CLAUDE_LOG"]
with open(log, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(record) + "\n")
n = sum(1 for _ in open(log, encoding="utf-8"))
script = json.load(open(os.environ["FAKE_CLAUDE_SCRIPT"], encoding="utf-8"))
resp = script[min(n - 1, len(script) - 1)] if script else {{}}
time.sleep(resp.get("sleep", 0))
done = os.environ.get("FAKE_CLAUDE_DONE")
if done:  # proves the process outlived (or not) a timeout
    with open(done, "a", encoding="utf-8") as fh:
        fh.write("finished\n")
sys.stdout.write(resp.get("stdout", ""))
sys.exit(resp.get("exit", 0))
'''

# A launcher that starts the fake as a CHILD (like npm's claude.cmd: cmd.exe -> node.exe).
WRAPPER = r'''#!{python}
import subprocess, sys
sys.exit(subprocess.call([sys.executable, {fake!r}] + sys.argv[1:]))
'''

# Stands in for node.exe: runs "cli.js" (the fake) with this interpreter.
NODE = r'''#!{python}
import os, sys
os.execv(sys.executable, [sys.executable] + sys.argv[1:])
'''


def _executable(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def result(text: str = "", structured=None, **extra) -> str:
    """One line of ``--output-format json`` output."""
    obj = {"type": "result", "subtype": "success", "is_error": False, "result": text,
           "num_turns": 2, "total_cost_usd": 0.0, "stop_reason": "end_turn", **extra}
    if structured is not None:
        obj["structured_output"] = structured
    return json.dumps(obj) + "\n"


class FakeClaude:
    """Controls and inspects the fake ``claude`` executable."""

    def __init__(self, root: Path, name: str = "claude"):
        self.root = root
        self.path = root / name
        self.log = root / "calls.jsonl"
        self.script = root / "script.json"
        self.probes = root / "probes.log"
        self.help = root / "help.txt"
        self.unknown = root / "unknown.json"
        self.help.write_text(HELP, encoding="utf-8")
        _executable(self.path, FAKE.format(python=sys.executable))
        self.respond([{"stdout": result("hi")}])

    def respond(self, responses: list[dict]) -> None:
        self.script.write_text(json.dumps(responses), encoding="utf-8")

    def lacks(self, *flags: str) -> None:
        """Act like an older Claude Code that does not know ``flags``."""
        self.unknown.write_text(json.dumps(list(flags)), encoding="utf-8")

    def probe_tags(self) -> list[str]:
        return self.probes.read_text().splitlines() if self.probes.exists() else []

    def calls(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def probe_count(self) -> int:
        return len(self.probes.read_text().splitlines()) if self.probes.exists() else 0


@pytest.fixture
def fake_claude(project_dir, tmp_path, monkeypatch) -> FakeClaude:
    root = tmp_path / "fakebin"
    root.mkdir()
    fake = FakeClaude(root)
    monkeypatch.setenv("AGENCY_CLAUDE_CLI", str(fake.path))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(fake.log))
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(fake.script))
    monkeypatch.setenv("FAKE_CLAUDE_PROBES", str(fake.probes))
    monkeypatch.setenv("FAKE_CLAUDE_HELP", str(fake.help))
    monkeypatch.setenv("FAKE_CLAUDE_UNKNOWN", str(fake.unknown))
    fake.lacks()
    monkeypatch.delenv("AGENCY_MOCK_LLM", raising=False)
    llm_cli.clear_cache()
    yield fake
    llm_cli.clear_cache()


def write_config(project_dir: Path, **llm) -> "object":
    from agency.config import load_settings

    path = project_dir / "config.yaml"
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config.setdefault("llm", {}).update(llm)
    path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
    return load_settings(path)


def arg(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


SCHEMA = {"type": "object", "properties": {"score": {"type": "integer"},
                                           "why": {"type": "string"}},
          "required": ["score", "why"]}


# --------------------------------------------------------------------------- selection
def test_auto_selection_order(fake_claude, project_dir, monkeypatch):
    settings = write_config(project_dir)
    monkeypatch.setenv("AGENCY_MOCK_LLM", "1")
    assert LLM(settings).backend == "mock" and LLM(settings).mock is True
    monkeypatch.delenv("AGENCY_MOCK_LLM")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert LLM(settings).backend == "api" and LLM(settings).mock is False
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    llm = LLM(settings)
    assert llm.backend == "claude_cli" and llm.mock is False
    assert fake_claude.calls() == []  # detection is free: no model call
    assert fake_claude.probe_tags() == ["--version", "--help", "-p check"]
    LLM(settings).backend  # cached: no second probe
    assert fake_claude.probe_count() == 3
    monkeypatch.setenv("AGENCY_CLAUDE_CLI", "off")
    assert LLM(settings).backend == "mock"


def test_explicit_backends(fake_claude, project_dir, monkeypatch):
    assert LLM(write_config(project_dir, backend="mock")).backend == "mock"
    assert LLM(write_config(project_dir, backend="api")).backend == "mock"  # no key
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert LLM(write_config(project_dir, backend="claude_cli")).backend == "claude_cli"
    assert LLM(write_config(project_dir, backend="nonsense")).backend == "api"  # = auto


def test_too_old_cli_is_not_used(fake_claude, project_dir):
    fake_claude.help.write_text("Usage: claude\n  --output-format <format>\n", encoding="utf-8")
    assert LLM(write_config(project_dir)).backend == "mock"


def test_configured_claude_path_is_found(fake_claude, project_dir, monkeypatch, tmp_path):
    monkeypatch.delenv("AGENCY_CLAUDE_CLI")
    monkeypatch.setattr(llm_cli.shutil, "which", lambda _name: None)
    monkeypatch.setattr(llm_cli.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    assert llm_cli.find_claude(write_config(project_dir)) is None
    llm_cli.clear_cache()
    info = llm_cli.find_claude(write_config(project_dir, claude_path=str(fake_claude.path)))
    assert info is not None and info.path == str(fake_claude.path)
    assert info.version == "2.1.302" and info.supports("--json-schema")


# --------------------------------------------------------------------------- one call
PAID_OR_SECRET_ENV = {
    "ANTHROPIC_API_KEY": "sk-ant-paid-key-123456",
    "ANTHROPIC_AUTH_TOKEN": "paid-bearer-token-777",  # ranked above the owner's login
    "ANTHROPIC_CUSTOM_HEADERS": "X-Bill-To: someone-else",
    "ANTHROPIC_BASE_URL": "https://gateway.example",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1",
    "AWS_BEARER_TOKEN_BEDROCK": "bedrock-key",
    "ZERNIO_API_KEY": "zr_secret_value_999",
    "YOUTUBE_OAUTH_JSON": '{"refresh_token": "1//x"}',  # an adapter's optional secret
    "DISCORD_WEBHOOK_URL": "https://discord.example/hook",
    "GITHUB_TOKEN": "ghp_not_for_the_child",
}


def test_complete_flags_stdin_and_child_env(fake_claude, project_dir, monkeypatch):
    for name, value in PAID_OR_SECRET_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AGENCY_TEST_HARMLESS", "kept")
    llm = LLM(write_config(project_dir, backend="claude_cli"))
    fake_claude.respond([{"stdout": result("שלום מ-Claude")}])
    assert llm.complete("SYSTEM PROMPT", "hello prompt") == "שלום מ-Claude"
    (call,) = fake_claude.calls()
    argv = call["argv"]
    assert argv[0] == "-p" and arg(argv, "--output-format") == "json"
    for flag in ("--no-session-persistence", "--strict-mcp-config", "--safe-mode"):
        assert flag in argv
    assert "--bare" not in argv  # it would ignore the owner's login (API key only)
    assert arg(argv, "--tools") == ""
    assert arg(argv, "--model") == "claude-opus-5-5"
    # hidden from the real --help, found by the free check: always sent
    assert "--system-prompt" not in argv and call["sysfile"] == "SYSTEM PROMPT"
    assert int(arg(argv, "--max-turns")) == 2
    assert arg(argv, "--setting-sources") == "project"
    assert set(arg(argv, "--disallowedTools").split(",")) >= {
        "Bash", "Edit", "Write", "WebFetch", "WebSearch", "NotebookEdit", "Agent"}
    assert "--json-schema" not in argv and "--add-dir" not in argv
    assert call["stdin"] == "hello prompt"  # the prompt goes on stdin, not argv
    assert "hello prompt" not in " ".join(argv)
    for name in PAID_OR_SECRET_ENV:  # his plan, never a paid key, provider or agency secret
        assert name not in call["env_names"], name
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] is None
    assert call["env"]["AGENCY_TEST_HARMLESS"] == "kept"
    assert "PATH" in call["env_names"] and "HOME" in call["env_names"]
    assert not Path(call["cwd"]).exists()  # private temp dir, removed afterwards
    assert not Path(call["cwd"]).is_relative_to(project_dir)


def test_withheld_names():
    for name in (*PAID_OR_SECRET_ENV, "CLAUDE_CODE_OAUTH_TOKEN", "X_API_SECRET",
                 "REDDIT_PASSWORD", "TELEGRAM_BOT_TOKEN", "META_ACCESS_TOKEN", "anthropic_api_key",
                 "AWS_SECRET_ACCESS_KEY", "OPENAI_API_KEY", "ANTHROPIC_MODEL"):
        assert llm_cli.withheld(name), name
    for name in ("PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SystemRoot", "TEMP",
                 "HTTPS_PROXY", "NO_PROXY", "NODE_EXTRA_CA_CERTS", "CLAUDE_CONFIG_DIR",
                 "TOKENIZERS_PARALLELISM", "SSH_AUTH_SOCK", "LANG", "AGENCY_KEYRING"):
        assert not llm_cli.withheld(name), name
    env = llm_cli.child_env("sk-ant-oat01-login")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-login"  # the one we hand over


def test_oauth_token_only_when_resolvable(fake_claude, project_dir, fake_keyring):
    llm = LLM(write_config(project_dir))
    llm.complete("s", "u")
    assert fake_claude.calls()[-1]["env"]["CLAUDE_CODE_OAUTH_TOKEN"] is None
    fake_keyring.store[("cs2studio", "claude_token")] = "sk-ant-oat01-studio-token"
    from agency.config import clear_secret_cache, secret_source
    clear_secret_cache()
    assert secret_source("CLAUDE_CODE_OAUTH_TOKEN") == "studio"  # read-only alias
    llm.complete("s", "u")
    call = fake_claude.calls()[-1]
    assert call["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-studio-token"
    assert "sk-ant-oat01-studio-token" not in " ".join(call["argv"])  # env only
    assert fake_keyring.store[("cs2studio", "claude_token")] == "sk-ant-oat01-studio-token"


def test_refused_saved_token_retries_with_the_login(fake_claude, project_dir, fake_keyring):
    fake_keyring.store[("cs2studio", "claude_token")] = "expired-token"
    llm = LLM(write_config(project_dir))
    fake_claude.respond([{"stdout": result("Authentication error · invalid token",
                                           is_error=True)},
                         {"stdout": result("ok")}])
    assert llm.complete("s", "u") == "ok"
    first, second = fake_claude.calls()
    assert first["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "expired-token"
    assert second["env"]["CLAUDE_CODE_OAUTH_TOKEN"] is None


def test_unavailable_model_retries_with_the_default(fake_claude, project_dir):
    llm = LLM(write_config(project_dir))
    fake_claude.respond([{"stdout": result("model claude-opus-5-5 is not available on your plan",
                                           is_error=True)},
                         {"stdout": result("ok")}])
    assert llm.complete("s", "u") == "ok"
    first, second = fake_claude.calls()
    assert "--model" in first["argv"] and "--model" not in second["argv"]


def test_complete_json_uses_structured_output(fake_claude, project_dir):
    llm = LLM(write_config(project_dir))
    fake_claude.respond([{"stdout": result('{"score": 1}', {"score": 9, "why": "clutch"})}])
    out = llm.complete_json("s", "rate it", SCHEMA, mock_value={"score": 0, "why": "mock"})
    assert out == {"score": 9, "why": "clutch"}
    argv = fake_claude.calls()[0]["argv"]
    schema = json.loads(arg(argv, "--json-schema"))
    assert schema["additionalProperties"] is False and schema["required"] == ["score", "why"]
    assert int(arg(argv, "--max-turns")) >= 2  # structured output needs 2+ turns


def test_complete_json_falls_back_to_the_text(fake_claude, project_dir):
    llm = LLM(write_config(project_dir))
    text = 'Here you go:\n```json\n{"score": 7, "why": "ace"}\n```'
    fake_claude.respond([{"stdout": result(text)}])
    assert llm.complete_json("s", "u", SCHEMA, mock_value={}) == {"score": 7, "why": "ace"}


def test_complete_json_retries_once_then_fails_cleanly(fake_claude, project_dir):
    llm = LLM(write_config(project_dir))
    fake_claude.respond([{"stdout": result("", {"score": 3})},
                         {"stdout": result("", {"score": 4, "why": "fixed"})}])
    assert llm.complete_json("s", "u", SCHEMA, mock_value={}) == {"score": 4, "why": "fixed"}
    assert "previous answer did not match" in fake_claude.calls()[1]["stdin"]
    fake_claude.log.unlink()
    fake_claude.respond([{"stdout": result("no json here")}])
    with pytest.raises(LLMError) as err:
        llm.complete_json("s", "u", SCHEMA, mock_value={})
    assert len(fake_claude.calls()) == 2 and "{" not in str(err.value)


def test_errors_become_plain_llm_errors(fake_claude, project_dir, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "tok-should-never-show")
    llm = LLM(write_config(project_dir))
    fake_claude.respond([{"stdout": result("Authentication error · please run /login",
                                           is_error=True)}])
    with pytest.raises(LLMError) as err:
        llm.complete("s", "u")
    assert "Claude" in str(err.value) and "tok-should" not in str(err.value)
    assert len(fake_claude.calls()) == 1  # token from the environment: no retry
    fake_claude.respond([{"stdout": "panic: something broke\n", "exit": 3}])
    with pytest.raises(LLMError, match="קוד 3"):
        llm.complete("s", "u")
    fake_claude.respond([{"stdout": result("", stop_reason="refusal")}])
    with pytest.raises(LLMRefusal):
        llm.complete("s", "u")
    fake_claude.respond([{"stdout": json.dumps({"type": "result", "subtype": "error_max_turns",
                                                "is_error": True}) + "\n"}])
    with pytest.raises(LLMError, match="לא הספיק"):
        llm.complete("s", "u")


def test_timeout_is_an_llm_error(fake_claude, project_dir):
    llm = LLM(write_config(project_dir, cli_timeout_s=1))
    fake_claude.respond([{"stdout": result("late"), "sleep": 5}])
    with pytest.raises(LLMError, match="1 שניות"):
        llm.complete("s", "u")


def test_warnings_before_the_json_line(fake_claude, project_dir):
    llm = LLM(write_config(project_dir))
    noisy = ("Warning: settings file ignored\n{not json at all}\n"
             + result("", {"score": 5, "why": "w"}) + "\n")
    fake_claude.respond([{"stdout": noisy}])
    assert llm.complete_json("s", "u", SCHEMA, mock_value={}) == {"score": 5, "why": "w"}


def test_parse_output_shapes():
    one = json.loads(result("x"))
    assert llm_cli.parse_output("warn\n" + json.dumps(one)) == one
    assert llm_cli.parse_output(json.dumps(one, indent=2)) == one
    arr = [{"type": "system"}, {"type": "assistant"}, one]
    assert llm_cli.parse_output(json.dumps(arr)) == one
    assert llm_cli.parse_output("nothing here") is None
    assert llm_cli.extract_json('so {"a": {"b": 1}} and {"c": 2}') == {"a": {"b": 1}}


# --------------------------------------------------------------------------- vision
def test_vision_call_reads_frame_copies_only(fake_claude, project_dir, tmp_path):
    frames = []
    for i in range(2):
        f = tmp_path / f"orig_{i}.jpg"
        f.write_bytes(b"\xff\xd8\xff\xe0fakejpeg" + bytes([i]))
        frames.append(f)
    llm = LLM(write_config(project_dir))
    fake_claude.respond([{"stdout": result("", {"score": 8, "why": "ace"})}])
    content = [LLM.image_block(f) for f in frames] + [{"type": "text", "text": "Analyse it."}]
    assert llm.complete_json("s", content, SCHEMA, mock_value={})["score"] == 8
    (call,) = fake_claude.calls()
    argv = call["argv"]
    assert arg(argv, "--tools") == "Read"
    folder = Path(arg(argv, "--add-dir"))
    assert folder.is_relative_to(Path(call["cwd"]))  # inside the private temp dir
    assert "--allowedTools" not in argv  # Read stays limited to the working folders
    assert "Bash" in arg(argv, "--disallowedTools")
    assert call["frames_exist"] and len(call["frames"]) == 2
    for frame in call["frames"]:
        assert frame in call["stdin"]  # absolute paths of the copies in the prompt
    assert str(frames[0]) not in call["stdin"]  # never the originals
    assert "Analyse it." in call["stdin"] and "base64" not in call["stdin"]
    assert int(arg(argv, "--max-turns")) >= 6
    assert not folder.exists()


def test_api_mode_strips_the_private_path(tmp_path):
    from agency.llm import _api_content

    f = tmp_path / "f.png"
    f.write_bytes(b"\x89PNG\r\n")
    block = LLM.image_block(f)
    assert block["_path"] == str(f.resolve())
    (clean,) = _api_content([block])
    assert "_path" not in clean and clean["source"]["media_type"] == "image/png"


# --------------------------------------------------------------------------- daily cap + log
def test_daily_cap_falls_back_to_mock(fake_claude, project_dir):
    settings = write_config(project_dir, cli_daily_calls=2)
    llm = LLM(settings)
    fake_claude.respond([{"stdout": result("", {"score": 9, "why": "real"})}])
    mock = {"score": 0, "why": "mock"}
    assert llm.complete_json("s", "u", SCHEMA, mock_value=mock)["why"] == "real"
    assert llm.complete_json("s", "u", SCHEMA, mock_value=mock)["why"] == "real"
    assert llm.complete_json("s", "u", SCHEMA, mock_value=mock) == mock
    assert llm.complete_json("s", "u", SCHEMA, mock_value=mock) == mock
    assert len(fake_claude.calls()) == 2
    assert llm.mock is True and llm.backend == "claude_cli" and llm.resting()
    from agency.db import DB
    db = DB(settings.db_path)
    rests = [e for e in db.list_events() if "המוח נח להיום" in e["message"]]
    assert len(rests) == 1  # once per day
    # a new day: the brain is back
    usage = db.kv_get("llm.cli_calls")
    db.kv_set("llm.cli_calls", {**usage, "day": "2000-01-01"})
    assert llm.mock is False
    assert llm.complete_json("s", "u", SCHEMA, mock_value=mock)["why"] == "real"
    assert llm.brain_info()["calls_today"] == 1


def test_each_call_is_logged_without_prompt_content(fake_claude, project_dir, ctx):
    from agency.staff.base import StaffMember

    class Writer(StaffMember):
        name = "copywriter"

        def write(self):
            return self.llm.complete("SECRET-SYSTEM", "PRIVATE-PROMPT-TEXT")

    ctx.llm = LLM(write_config(project_dir))
    Writer(ctx).write()
    events = [e for e in ctx.db.list_events() if e["actor"] == "brain"]
    assert len(events) == 1
    msg = events[0]["message"]
    assert "claude_cli" in msg and "copywriter.write" in msg and "ok" in msg
    assert "PRIVATE-PROMPT-TEXT" not in msg and "SECRET-SYSTEM" not in msg


# --------------------------------------------------------------------------- manager
def test_manager_chat_is_one_call_with_a_snapshot(fake_claude, project_dir, ctx, tiny_video):
    from agency.staff.manager import Manager

    ctx.llm = LLM(write_config(project_dir))
    vid = ctx.db.add_video(str(tiny_video), "Deagle 3K on Mirage")
    from agency.models import VideoStatus
    ctx.db.update_video(vid, status=VideoStatus.AWAITING_APPROVAL)
    fake_claude.respond([{"stdout": result("הכל רץ, אחי. מחכה לך קליפ אחד.")}])
    reply = Manager(ctx).chat("מה המצב?")
    assert reply == "הכל רץ, אחי. מחכה לך קליפ אחד."
    (call,) = fake_claude.calls()  # ONE call
    assert arg(call["argv"], "--tools") == ""
    stdin = call["stdin"]
    assert "REAL DATA SNAPSHOT" in stdin and "מה המצב?" in stdin
    for name in ("get_status", "list_approvals", "budget_status", "list_videos", "list_posts",
                 f"explain_plan(video_id={vid})"):
        assert name in stdin, name
    assert "Deagle 3K on Mirage" in stdin
    assert "no tools" in call["sysfile"]
    # tools that change something never run on their own
    assert ctx.db.list_approvals(None) == []
    names = {t.name for t in Manager(ctx).tools()}
    assert not any("approve" in n for n in names)
    assert {t.name for t in Manager(ctx).tools() if not t.default_inputs()} >= {
        "run_analytics", "propose_promotion", "schedule_reschedule"}


def test_tool_snapshot_defaults():
    ran = []
    tools = [Tool("a", "a", fn=lambda i: ran.append(("a", i)) or {"x": 1}),
             Tool("b", "b", {"type": "object", "properties": {"id": {"type": "integer"}},
                             "required": ["id"]}, fn=lambda i: ran.append(("b", i))),
             Tool("c", "c", fn=lambda i: ran.append(("c", i)), snapshot_inputs=[]),
             Tool("d", "d", fn=lambda i: 1 / 0)]
    snap = LLM.tool_snapshot(tools)
    assert snap["a"] == {"x": 1} and snap["d"] == {"error": "ZeroDivisionError"}
    assert ran == [("a", {})]


# --------------------------------------------------------------------------- surfaces
def test_settings_api_shows_the_brain_read_only(fake_claude, project_dir, ctx):
    from fastapi.testclient import TestClient

    from agency.dashboard.app import create_app

    ctx.llm = LLM(write_config(project_dir))
    app = create_app(ctx)
    with TestClient(app, base_url="http://127.0.0.1:8765", client=("127.0.0.1", 50000),
                    headers={"X-Agency-Request": "1"}) as c:
        data = c.get("/api/settings").json()
        assert data["llm_backend"] == "claude_cli"
        assert data["info"]["llm"]["label_he"] == "Claude (המנוי שלך)"
        assert "path" not in data["info"]["llm"]
        assert c.get("/api/status").json()["llm_backend"] == "claude_cli"
        res = c.put("/api/settings", json={"llm_backend": "api"})
        assert res.status_code == 400  # read-only: nothing to save
    assert fake_claude.calls() == []


def test_settings_api_mock_label(ctx):
    from fastapi.testclient import TestClient

    from agency.dashboard.app import create_app

    with TestClient(create_app(ctx), base_url="http://127.0.0.1:8765",
                    client=("127.0.0.1", 50000), headers={"X-Agency-Request": "1"}) as c:
        data = c.get("/api/settings").json()
    assert data["llm_backend"] == "mock" and data["info"]["llm"]["label_he"] == "מצב דמו"


def test_cli_brain_command_in_english(fake_claude, project_dir, monkeypatch):
    from typer.testing import CliRunner

    from agency import cli

    write_config(project_dir)
    monkeypatch.setenv("AGENCY_CLI_LANG", "en")
    monkeypatch.setattr(cli, "_state", {"config": None, "ctx": None})
    out = CliRunner().invoke(cli.app, ["brain"])
    assert out.exit_code == 0, out.output
    assert "Team brain: Claude Code (your plan)" in out.output
    assert "2.1.302" in out.output and "Calls today: 0 of 60" in out.output
    assert not any("֐" <= ch <= "׿" for ch in out.output)
    out = CliRunner().invoke(cli.app, ["platforms"])
    assert out.exit_code == 0 and "Team brain: Claude Code (your plan)" in out.output
    assert fake_claude.calls() == []


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="ffmpeg not installed")
def test_analyst_sees_the_frames_through_the_cli(fake_claude, project_dir, ctx, tiny_video):
    from agency.staff.analyst import VideoAnalyst

    ctx.llm = LLM(write_config(project_dir))
    analysis = {"clip_type": "clutch", "map": "Mirage", "weapons": ["Desert Eagle"],
                "highlights": [{"t": 1.0, "desc": "3K"}], "hook_seconds": 0.5, "mood": "hype",
                "summary_he": "קלאץ'", "summary_en": "A deagle clutch.",
                "best_short_window": {"start": 0, "end": 3}, "audience": ["CS2 players"],
                "score_1_10": 8, "ai_shots": False}
    fake_claude.respond([{"stdout": result("", analysis)}])
    vid = ctx.db.add_video(str(tiny_video), "deagle clutch")
    out = VideoAnalyst(ctx).analyze(ctx.db.get_video(vid))
    assert out["clip_type"] == "clutch" and out["map"] == "Mirage"
    (call,) = fake_claude.calls()
    assert arg(call["argv"], "--tools") == "Read" and len(call["frames"]) >= 1
    assert call["frames_exist"]
    assert all(f in call["stdin"] for f in call["frames"])
    assert "VideoAnalyst" not in call["stdin"] and "Video Analyst" in call["sysfile"]
    assert int(arg(call["argv"], "--max-turns")) >= 4  # the Read loop is capped
    assert any("analyst.analyze" in e["message"] for e in ctx.db.list_events()
               if e["actor"] == "brain")


def test_windows_cmd_launcher_gets_no_free_text_on_the_command_line(fake_claude, project_dir,
                                                                    monkeypatch):
    cmd = FakeClaude(fake_claude.root, "claude.cmd")  # same fake, .cmd name (Linux: shebang)
    monkeypatch.setenv("AGENCY_CLAUDE_CLI", str(cmd.path))
    llm_cli.clear_cache()
    llm = LLM(write_config(project_dir))
    assert llm.cli.info.is_cmd
    cmd.respond([{"stdout": result('{"score": 6, "why": "100% & more"}')}])
    out = llm.complete_json('Say "hi" & 100% ^ok', "u", SCHEMA, mock_value={})
    assert out == {"score": 6, "why": "100% & more"}
    (call,) = cmd.calls()
    assert "--system-prompt" not in call["argv"] and "--json-schema" not in call["argv"]
    assert call["sysfile"].startswith('Say "hi" & 100% ^ok') and '"required"' in call["sysfile"]
    llm2 = LLM(write_config(project_dir, model="opus&calc"))
    with pytest.raises(LLMError, match="תווים מיוחדים"):
        llm2.complete("s", "u")
    assert len(cmd.calls()) == 1  # refused before starting anything


# --------------------------------------------------------------------------- hidden flags
def test_real_help_hides_the_flags_but_the_free_check_finds_them(fake_claude):
    listed = set(llm_cli._FLAG_RE.findall(HELP))  # the real 2.1.302 --help
    assert {"--output-format", "--tools", "--json-schema", "--system-prompt"} <= listed
    assert "--max-turns" not in listed and "--system-prompt-file" not in listed
    info = llm_cli.probe(fake_claude.path)
    assert info is not None and info.version == "2.1.302"
    assert info.supports("--max-turns") and info.supports("--system-prompt-file")
    assert fake_claude.calls() == []  # the check stops at the missing file: no model call
    assert fake_claude.probe_tags() == ["--version", "--help", "-p check"]
    args = llm_cli.ClaudeCLI(info).build_args([], schema=None, read_dir=Path("/tmp"),
                                              max_turns=5)
    assert arg(args, "--max-turns") == "5" and arg(args, "--tools") == "Read"


def test_a_huge_system_prompt_never_goes_on_the_command_line(fake_claude, project_dir):
    llm = LLM(write_config(project_dir))
    system = "Brand rule: no blocked words. " * 1500  # ~45k chars, over Windows' 32,767 limit
    assert llm.complete(system, "u") == "hi"
    (call,) = fake_claude.calls()
    assert call["sysfile"] == system
    assert len(" ".join(call["argv"])) < 2000
    assert not Path(arg(call["argv"], "--system-prompt-file")).exists()  # temp dir removed


def test_older_cli_without_the_hidden_flags(fake_claude, project_dir):
    fake_claude.lacks("--max-turns", "--system-prompt-file")
    llm = LLM(write_config(project_dir))
    assert llm.backend == "claude_cli"
    assert fake_claude.probe_tags() == ["--version", "--help", "unknown --max-turns",
                                        "unknown --system-prompt-file"]
    assert llm.complete("SHORT SYSTEM", "u") == "hi"  # the fake rejects flags it lacks
    (call,) = fake_claude.calls()
    assert arg(call["argv"], "--system-prompt") == "SHORT SYSTEM"
    assert "--max-turns" not in call["argv"] and "--system-prompt-file" not in call["argv"]
    with pytest.raises(LLMError, match="ישן מדי"):
        llm.complete("x" * 30000, "u")
    assert len(fake_claude.calls()) == 1  # refused before starting anything


def test_cli_without_max_turns_only(fake_claude):
    fake_claude.lacks("--max-turns")
    info = llm_cli.probe(fake_claude.path)
    assert not info.supports("--max-turns") and info.supports("--system-prompt-file")
    assert fake_claude.probe_tags()[2:] == ["unknown --max-turns", "-p check"]


def test_old_cmd_launcher_is_not_used(fake_claude, project_dir, monkeypatch):
    cmd = FakeClaude(fake_claude.root, "claude.cmd")
    fake_claude.lacks("--system-prompt-file")  # same fake, an older version
    monkeypatch.setenv("AGENCY_CLAUDE_CLI", str(cmd.path))
    llm_cli.clear_cache()
    llm = LLM(write_config(project_dir))
    assert llm.backend == "mock"  # honest demo mode, not a brain whose every call fails
    assert cmd.calls() == []


def test_npm_shim_runs_node_and_cli_js_without_cmd(fake_claude, project_dir, monkeypatch,
                                                   tmp_path):
    npm = tmp_path / "npm"
    shim = _executable(npm / "claude.cmd", FAKE.format(python=sys.executable))
    script = _executable(npm / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js",
                         FAKE.format(python=sys.executable))
    node = _executable(npm / "node.exe", NODE.format(python=sys.executable))
    monkeypatch.setenv("AGENCY_CLAUDE_CLI", str(shim))
    llm_cli.clear_cache()
    llm = LLM(write_config(project_dir))
    info = llm.cli.info
    assert info.path == str(node) and info.script == str(script) and not info.is_cmd
    assert info.argv == [str(node), str(script)] and info.shown_path == str(script)
    assert info.supports("--max-turns") and info.supports("--system-prompt-file")
    fake_claude.respond([{"stdout": result("", {"score": 6, "why": "100% & more"})}])
    out = llm.complete_json('Say "hi" & 100% ^ok', "u", SCHEMA, mock_value={})
    assert out == {"score": 6, "why": "100% & more"}
    (call,) = fake_claude.calls()
    assert "--json-schema" in call["argv"]  # no cmd.exe in front: nothing re-parses it
    assert call["sysfile"] == 'Say "hi" & 100% ^ok'
    # no node.exe next to the shim and none on PATH: the .cmd itself is the fallback
    node.unlink()
    monkeypatch.setattr(llm_cli.shutil, "which", lambda _name: None)
    llm_cli.clear_cache()
    assert llm_cli.find_claude(write_config(project_dir)).is_cmd


# --------------------------------------------------------------------------- process tree
def test_timeout_stops_the_whole_process_tree(fake_claude, project_dir, monkeypatch,
                                              tmp_path):
    wrapper = _executable(tmp_path / "launcher" / "claude",
                          WRAPPER.format(python=sys.executable, fake=str(fake_claude.path)))
    done = tmp_path / "done.txt"
    monkeypatch.setenv("AGENCY_CLAUDE_CLI", str(wrapper))
    monkeypatch.setenv("FAKE_CLAUDE_DONE", str(done))
    llm_cli.clear_cache()
    llm = LLM(write_config(project_dir, cli_timeout_s=2))
    assert llm.backend == "claude_cli"  # detection runs through the launcher too
    fake_claude.respond([{"stdout": result("late"), "sleep": 4}])
    start = time.monotonic()
    with pytest.raises(LLMError, match="2 שניות"):
        llm.complete("s", "u")
    assert time.monotonic() - start < 3.5  # the timeout holds
    assert len(fake_claude.calls()) == 1  # the grandchild had started
    time.sleep(max(0.0, start + 5.0 - time.monotonic()))
    assert not done.exists()  # ...and was stopped, not left running


def test_kill_tree_on_windows_uses_taskkill(monkeypatch):
    ran: list = []
    monkeypatch.setattr(llm_cli.subprocess, "run", lambda args, **kw: ran.append((args, kw)))
    monkeypatch.setenv("SystemRoot", r"C:\Windows")

    class Proc:
        pid = 4242
        killed = False

        def kill(self):
            self.killed = True

    proc = Proc()
    llm_cli._kill_tree(proc, windows=True)
    ((args, kw),) = ran
    assert args[0].startswith("C:\\Windows") and args[0].endswith("taskkill.exe")
    assert args[1:] == ["/T", "/F", "/PID", "4242"]
    assert kw["timeout"] > 0 and not kw.get("shell")
    assert proc.killed


# --------------------------------------------------------------------------- daily cap (atomic)
def test_daily_cap_stops_a_retry_before_it_starts(fake_claude, project_dir):
    from agency.db import DB

    settings = write_config(project_dir, cli_daily_calls=1)
    llm = LLM(settings)
    fake_claude.respond([{"stdout": result("", {"score": 3})}])  # no "why": wants a retry
    mock = {"score": 0, "why": "mock"}
    assert llm.complete_json("s", "u", SCHEMA, mock_value=mock) == mock
    assert len(fake_claude.calls()) == 1  # the retry never started
    assert llm.brain_info()["calls_today"] == 1
    events = DB(settings.db_path).list_events()
    assert sum("המוח נח להיום" in e["message"] for e in events) == 1
    assert any(e["message"].endswith("rested") for e in events if e["actor"] == "brain")


def test_daily_cap_holds_for_concurrent_callers(fake_claude, project_dir):
    settings = write_config(project_dir, cli_daily_calls=2)
    llm = LLM(settings)
    assert llm.backend == "claude_cli"
    fake_claude.respond([{"stdout": result("real"), "sleep": 0.3}])
    barrier = threading.Barrier(6)
    answers: list[str] = []

    def worker() -> None:
        barrier.wait()
        answers.append(llm.complete("s", "u"))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert len(answers) == 6 and answers.count("real") == 2
    assert len(fake_claude.calls()) == 2 and llm.brain_info()["calls_today"] == 2
    other = LLM(settings)  # another LLM on the same DB (e.g. the CLI next to the dashboard)
    assert other.complete("s", "u") != "real" and len(fake_claude.calls()) == 2
