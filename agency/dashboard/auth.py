"""Optional phone access (SPEC_V2 §6): PIN login, session cookies, loopback / Host checks, QR.

Framework-agnostic: plain functions over the agency ``DB`` (kv table) and ``Settings``. The
dashboard middleware calls :func:`require_auth` and the login endpoint calls :func:`login`;
nothing here imports FastAPI.

Secrets policy: the PIN and session tokens are never stored, logged or returned after
creation. The DB keeps only a salted scrypt hash of the PIN and sha256 hashes of tokens.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import ipaddress
import json
import math
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
TEMPLATES_DIR = HERE / "templates"

# --------------------------------------------------------------------------- constants
PIN_LENGTH = 6
PIN_KEY = "auth.pin"                  # kv: {"algo","salt","hash","n","r","p","created_at"}
LIMITS_KEY = "auth.pin_limits"        # kv: {"ips": {key: entry}, "global": entry}
SESSIONS_KEY = "auth.sessions"        # kv: {sha256(token): {"created","expires","label"}}

SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024

MAX_FAILURES = 5                      # per IP (IPv6: per /64) inside the window
WINDOW_S = 10 * 60
LOCKOUT_S = 10 * 60                   # lockout after the 5th failure, counted from it
GLOBAL_MAX_FAILURES = 30              # all IPs together inside the window -> everyone locked
MAX_TRACKED_IPS = 500

SESSION_TTL_S = 30 * 24 * 3600
MAX_SESSIONS = 20
TOKEN_BYTES = 32

COOKIE_NAME = "agency_session"
# Starlette/FastAPI ``response.set_cookie(COOKIE_NAME, token, **COOKIE_KWARGS)``.
# Secure=False on purpose: phone access is plain http on the home LAN.
COOKIE_KWARGS: dict[str, Any] = {
    "max_age": SESSION_TTL_S,
    "path": "/",
    "secure": False,
    "httponly": True,
    "samesite": "strict",
}
# ``response.delete_cookie(COOKIE_NAME, **COOKIE_DELETE_KWARGS)`` on logout.
COOKIE_DELETE_KWARGS: dict[str, Any] = {"path": "/", "secure": False, "httponly": True,
                                        "samesite": "strict"}

LOGIN_PATH = "/login"
LOGIN_API_PATH = "/api/auth/login"
# Reachable by remote clients without a session (only while remote access is enabled).
PUBLIC_PATHS: frozenset[str] = frozenset({LOGIN_PATH, LOGIN_API_PATH, "/api/health"})

LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# Reason codes
REASON_REMOTE_DISABLED = "remote_disabled"
REASON_LOGIN_REQUIRED = "login_required"
REASON_INVALID_FORMAT = "invalid_format"
REASON_NO_PIN = "no_pin"
REASON_BAD_PIN = "bad_pin"
REASON_LOCKED = "locked"

# Owner-facing Hebrew messages (no jargon: these reach the phone screen).
MSG_REMOTE_DISABLED = "הגישה מהטלפון כבויה. אפשר להדליק אותה בהגדרות, במחשב."
MSG_LOGIN_REQUIRED = "צריך להיכנס עם הקוד שמופיע על מסך המחשב."
MSG_INVALID_FORMAT = "הקוד הוא 6 ספרות. נסה שוב."
MSG_NO_PIN = "עוד אין קוד כניסה. במחשב: הגדרות ← גישה מהטלפון."

_LAN_NETWORKS = tuple(ipaddress.ip_network(n) for n in
                      ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
# UDP connect() only picks a route (no packet is sent); these are private, never on the internet.
_ROUTE_PROBES = ("192.168.255.254", "10.255.255.254", "172.31.255.254")
_HOSTNAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}"
                          r"[a-z0-9])?)*$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")
_DIGIT_MAP = {**{0x0660 + i: str(i) for i in range(10)},   # Arabic-Indic
              **{0x06F0 + i: str(i) for i in range(10)},   # Extended Arabic-Indic (Persian)
              **{0xFF10 + i: str(i) for i in range(10)}}   # full-width

_LOCK = threading.RLock()


def _now() -> float:
    """Wall-clock seconds (tests monkeypatch this)."""
    return time.time()


def _iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(float(ts), timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- kv plumbing
def _kv_mutate(db: Any, key: str, fn: Callable[[Any], tuple[Any, Any]]) -> Any:
    """Atomically read-modify-write one kv value. ``fn(current) -> (new_value, result)``."""
    with _LOCK:
        immediate = getattr(db, "immediate", None)
        if immediate is None:  # duck-typed DB without transactions: process lock only
            new, result = fn(db.kv_get(key))
            db.kv_set(key, new)
            return result
        with immediate() as conn:
            row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            try:
                current = json.loads(row["value"]) if row and row["value"] else None
            except (TypeError, ValueError):
                current = None
            new, result = fn(current)
            conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, json.dumps(new, ensure_ascii=False, default=str)))
        return result


# --------------------------------------------------------------------------- PIN
def generate_pin() -> str:
    """A fresh random 6-digit PIN (``secrets``), zero-padded. Shown once, never stored."""
    return f"{secrets.randbelow(10 ** PIN_LENGTH):0{PIN_LENGTH}d}"


def normalize_pin(pin: Any) -> str | None:
    """``pin`` as 6 ASCII digits (spaces/dashes dropped, Arabic/full-width digits mapped),
    or None when it is not a 6-digit code."""
    if not isinstance(pin, str) or len(pin) > 32:
        return None
    text = pin.translate(_DIGIT_MAP)
    text = re.sub(r"[\s\-]", "", text)
    return text if re.fullmatch(rf"[0-9]{{{PIN_LENGTH}}}", text) else None


def _scrypt(pin: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    return hashlib.scrypt(pin.encode("ascii"), salt=salt, n=n, r=r, p=p, dklen=dklen,
                          maxmem=SCRYPT_MAXMEM)


def hash_pin(pin: str) -> dict:
    """Salted scrypt record for ``pin`` (what gets stored). Raises ValueError on a bad PIN."""
    clean = normalize_pin(pin)
    if clean is None:
        raise ValueError("PIN must be 6 digits")
    salt = secrets.token_bytes(16)
    digest = _scrypt(clean, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN)
    return {"algo": "scrypt", "salt": salt.hex(), "hash": digest.hex(), "n": SCRYPT_N,
            "r": SCRYPT_R, "p": SCRYPT_P, "created_at": _now()}


def _valid_record(rec: Any) -> bool:
    if not isinstance(rec, dict) or rec.get("algo") != "scrypt":
        return False
    try:
        n, r, p = int(rec["n"]), int(rec["r"]), int(rec["p"])
        bytes.fromhex(rec["salt"])
        digest = bytes.fromhex(rec["hash"])
    except (KeyError, TypeError, ValueError):
        return False
    return (2 <= n <= 2 ** 20 and n & (n - 1) == 0 and 1 <= r <= 32 and 1 <= p <= 16
            and 16 <= len(digest) <= 64)


def pin_matches(pin: str, record: dict) -> bool:
    """Constant-time check of ``pin`` against a stored :func:`hash_pin` record."""
    clean = normalize_pin(pin)
    if clean is None or not _valid_record(record):
        return False
    expected = bytes.fromhex(record["hash"])
    actual = _scrypt(clean, bytes.fromhex(record["salt"]), int(record["n"]), int(record["r"]),
                     int(record["p"]), len(expected))
    return hmac.compare_digest(actual, expected)


def set_pin(db: Any, pin: str) -> None:
    """Store only the salted hash of ``pin`` and reset the PIN rate limits."""
    record = hash_pin(pin)
    with _LOCK:
        db.kv_set(PIN_KEY, record)
        db.kv_set(LIMITS_KEY, _empty_limits())


def rotate_pin(db: Any) -> str:
    """Generate, store (hashed) and return a new PIN — show it once on the PC screen.

    Existing phone sessions stay valid; call :func:`revoke_all` to log them out too.
    """
    pin = generate_pin()
    set_pin(db, pin)
    return pin


def clear_pin(db: Any) -> None:
    """Forget the PIN (remote logins impossible until :func:`rotate_pin`)."""
    with _LOCK:
        db.kv_set(PIN_KEY, None)


def pin_is_set(db: Any) -> bool:
    return _valid_record(db.kv_get(PIN_KEY))


def pin_created_at(db: Any) -> str | None:
    rec = db.kv_get(PIN_KEY)
    return _iso(rec.get("created_at")) if _valid_record(rec) else None


# --------------------------------------------------------------------------- rate limiting
@dataclass(frozen=True)
class PinCheck:
    """Result of :func:`verify_pin`. ``reason`` is None on success."""

    ok: bool
    reason: str | None = None          # invalid_format | no_pin | bad_pin | locked
    attempts_left: int = 0
    retry_after_s: int = 0

    @property
    def locked_minutes(self) -> int:
        return max(1, math.ceil(self.retry_after_s / 60)) if self.retry_after_s > 0 else 0


def _empty_limits() -> dict:
    return {"ips": {}, "global": {"fails": [], "locked_until": 0}}


def _clean_entry(entry: Any, now: float) -> dict:
    """Drop failures outside the window; a finished lockout starts a clean slate."""
    if not isinstance(entry, dict):
        return {"fails": [], "locked_until": 0}
    try:
        locked_until = float(entry.get("locked_until") or 0)
        fails = [float(t) for t in (entry.get("fails") or [])]
    except (TypeError, ValueError):
        return {"fails": [], "locked_until": 0}
    if locked_until and now >= locked_until:
        return {"fails": [], "locked_until": 0}
    return {"fails": [t for t in fails if 0 <= now - t < WINDOW_S], "locked_until": locked_until}


def _clean_limits(state: Any, now: float) -> dict:
    if not isinstance(state, dict):
        return _empty_limits()
    ips_in = state.get("ips") if isinstance(state.get("ips"), dict) else {}
    ips = {}
    for key, entry in ips_in.items():
        clean = _clean_entry(entry, now)
        if clean["fails"] or clean["locked_until"]:
            ips[str(key)] = clean
    if len(ips) > MAX_TRACKED_IPS:  # keep the most recently active
        ranked = sorted(ips.items(), key=lambda kv: max(kv[1]["fails"] or [kv[1]["locked_until"]]),
                        reverse=True)
        ips = dict(ranked[:MAX_TRACKED_IPS])
    return {"ips": ips, "global": _clean_entry(state.get("global"), now)}


def _remaining(entry: dict | None, now: float) -> int:
    if not entry or not entry.get("locked_until"):
        return 0
    return max(0, math.ceil(float(entry["locked_until"]) - now))


def _parse_ip(value: Any) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    raw = raw.split("%", 1)[0]  # IPv6 zone id
    try:
        return ipaddress.ip_address(raw)
    except ValueError:
        return None


def ip_key(client_ip: str | None) -> str:
    """Rate-limit bucket for a client: the IPv4 address (IPv4-mapped IPv6 unwrapped), or the
    IPv6 /64 (one device can own many addresses inside it)."""
    ip = _parse_ip(client_ip)
    if ip is None:
        raw = (client_ip or "").strip()[:64] if isinstance(client_ip, str) else ""
        return f"other:{raw}" if raw else "unknown"
    if ip.version == 6:
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def _record_failure(db: Any, key: str, now: float) -> PinCheck:
    def fn(current: Any) -> tuple[dict, PinCheck]:
        state = _clean_limits(current, now)
        entry = state["ips"].setdefault(key, {"fails": [], "locked_until": 0})
        entry["fails"].append(now)
        glob = state["global"]
        glob["fails"] = (glob["fails"] + [now])[-GLOBAL_MAX_FAILURES:]
        if len(entry["fails"]) >= MAX_FAILURES:
            entry["locked_until"] = now + LOCKOUT_S
        if len(glob["fails"]) >= GLOBAL_MAX_FAILURES:
            glob["locked_until"] = now + LOCKOUT_S
        wait = max(_remaining(entry, now), _remaining(glob, now))
        if wait:
            return state, PinCheck(False, REASON_LOCKED, 0, wait)
        return state, PinCheck(False, REASON_BAD_PIN, MAX_FAILURES - len(entry["fails"]))
    return _kv_mutate(db, LIMITS_KEY, fn)


def _record_success(db: Any, key: str, now: float) -> None:
    def fn(current: Any) -> tuple[dict, None]:
        state = _clean_limits(current, now)
        state["ips"].pop(key, None)
        return state, None
    _kv_mutate(db, LIMITS_KEY, fn)


def lockout_remaining(db: Any, client_ip: str | None) -> int:
    """Seconds until ``client_ip`` may try a PIN again (0 = not locked)."""
    now = _now()
    state = _clean_limits(db.kv_get(LIMITS_KEY), now)
    return max(_remaining(state["global"], now), _remaining(state["ips"].get(ip_key(client_ip)), now))


def verify_pin(db: Any, pin: Any, client_ip: str | None) -> PinCheck:
    """Check a PIN typed on a phone, with rate limiting.

    5 wrong PINs from one IP (IPv6: one /64) within 10 minutes -> that IP is locked for 10
    minutes from the 5th failure; 30 wrong PINs from all IPs together -> every remote login is
    locked for 10 minutes. While locked, even the right PIN is refused (and not even hashed).
    A malformed PIN is refused without counting; a correct PIN clears the IP's failures.
    """
    key = ip_key(client_ip)
    with _LOCK:  # serialises check-then-record (and bounds scrypt work to one at a time)
        now = _now()
        state = _clean_limits(db.kv_get(LIMITS_KEY), now)
        entry = state["ips"].get(key)
        wait = max(_remaining(state["global"], now), _remaining(entry, now))
        if wait:
            return PinCheck(False, REASON_LOCKED, 0, wait)
        left = MAX_FAILURES - len(entry["fails"]) if entry else MAX_FAILURES
        clean = normalize_pin(pin)
        if clean is None:
            return PinCheck(False, REASON_INVALID_FORMAT, left)
        record = db.kv_get(PIN_KEY)
        if not _valid_record(record):
            return PinCheck(False, REASON_NO_PIN, left)
        if pin_matches(clean, record):
            _record_success(db, key, now)
            return PinCheck(True, None, MAX_FAILURES)
        return _record_failure(db, key, now)


# --------------------------------------------------------------------------- sessions
def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _plausible_token(token: Any) -> bool:
    return isinstance(token, str) and 20 <= len(token) <= 128 and token.isascii()


def _live_sessions(current: Any, now: float) -> dict:
    out = {}
    if isinstance(current, dict):
        for h, meta in current.items():
            try:
                if isinstance(meta, dict) and float(meta.get("expires", 0)) > now:
                    out[str(h)] = meta
            except (TypeError, ValueError):
                continue
    return out


def _clean_label(label: Any) -> str:
    text = "".join(ch for ch in str(label or "") if ch.isprintable())
    return text.strip()[:120]


def create_session(db: Any, *, label: str = "") -> str:
    """New session token (``secrets.token_urlsafe(32)``) valid for 30 days.

    Only sha256(token) is stored. ``label`` (e.g. a shortened user agent) helps the owner
    recognise devices; it must not contain secrets. Keeps at most 20 sessions (oldest dropped).
    """
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = _now()

    def fn(current: Any) -> tuple[dict, None]:
        sessions = _live_sessions(current, now)
        sessions[_token_hash(token)] = {"created": now, "expires": now + SESSION_TTL_S,
                                        "label": _clean_label(label)}
        if len(sessions) > MAX_SESSIONS:
            newest = sorted(sessions.items(), key=lambda kv: float(kv[1].get("created", 0)),
                            reverse=True)[:MAX_SESSIONS]
            sessions = dict(newest)
        return sessions, None

    _kv_mutate(db, SESSIONS_KEY, fn)
    return token


def validate_session(db: Any, token: Any) -> bool:
    """True when ``token`` is a known, unexpired session."""
    if not _plausible_token(token):
        return False
    sessions = db.kv_get(SESSIONS_KEY)
    if not isinstance(sessions, dict):
        return False
    meta = sessions.get(_token_hash(token))
    if not isinstance(meta, dict):
        return False
    try:
        return float(meta.get("expires", 0)) > _now()
    except (TypeError, ValueError):
        return False


def revoke_session(db: Any, token: Any) -> bool:
    """Log out one device. True if the session existed."""
    if not _plausible_token(token):
        return False
    digest = _token_hash(token)
    now = _now()

    def fn(current: Any) -> tuple[dict, bool]:
        sessions = _live_sessions(current, now)
        return sessions, sessions.pop(digest, None) is not None

    return bool(_kv_mutate(db, SESSIONS_KEY, fn))


def revoke_all(db: Any) -> int:
    """"התנתק מכל המכשירים": drop every session. Returns how many live ones were dropped."""
    now = _now()
    return int(_kv_mutate(db, SESSIONS_KEY, lambda cur: ({}, len(_live_sessions(cur, now)))))


def session_count(db: Any) -> int:
    """Number of live (unexpired) sessions."""
    return len(_live_sessions(db.kv_get(SESSIONS_KEY), _now()))


# --------------------------------------------------------------------------- login flow
@dataclass
class LoginOutcome:
    """What the ``POST /api/auth/login`` endpoint should answer.

    ``token`` is set only on success: put it in the cookie, never in the body.
    """

    status: int
    body: dict
    token: str | None = None
    headers: dict = field(default_factory=dict)


def minutes_he(minutes: int) -> str:
    return "בעוד דקה" if minutes <= 1 else f"בעוד {minutes} דקות"


def check_message(check: PinCheck) -> str:
    """Friendly Hebrew text for a failed :class:`PinCheck`."""
    if check.reason == REASON_LOCKED:
        return f"יותר מדי ניסיונות. אפשר לנסות שוב {minutes_he(check.locked_minutes)}."
    if check.reason == REASON_BAD_PIN:
        if check.attempts_left == 1:
            return "הקוד לא נכון. נשאר עוד ניסיון אחד."
        return f"הקוד לא נכון. נשארו עוד {check.attempts_left} ניסיונות."
    if check.reason == REASON_NO_PIN:
        return MSG_NO_PIN
    if check.reason == REASON_INVALID_FORMAT:
        return MSG_INVALID_FORMAT
    return ""


def login(db: Any, pin: Any, client_ip: str | None, *, label: str = "") -> LoginOutcome:
    """Full PIN login: verify (rate limited) and, on success, create a session.

    Bodies (never contain the PIN or token):
      200 {"ok": true}
      400 {"ok": false, "reason": "invalid_format", "detail", "attempts_left"}
      401 {"ok": false, "reason": "bad_pin", "detail", "attempts_left"}
      403 {"ok": false, "reason": "no_pin", "detail"}
      429 {"ok": false, "reason": "locked", "detail", "retry_after_s", "locked_minutes"}
          + header Retry-After
    """
    check = verify_pin(db, pin, client_ip)
    if check.ok:
        return LoginOutcome(200, {"ok": True}, create_session(db, label=label))
    body: dict[str, Any] = {"ok": False, "reason": check.reason, "detail": check_message(check)}
    if check.reason == REASON_LOCKED:
        body.update(retry_after_s=check.retry_after_s, locked_minutes=check.locked_minutes)
        return LoginOutcome(429, body, None, {"Retry-After": str(check.retry_after_s)})
    if check.reason == REASON_NO_PIN:
        return LoginOutcome(403, body)
    body["attempts_left"] = check.attempts_left
    return LoginOutcome(400 if check.reason == REASON_INVALID_FORMAT else 401, body)


# --------------------------------------------------------------------------- who is asking
def is_loopback(client_host: str | None) -> bool:
    """True only for a loopback IP literal: 127.0.0.0/8, ``::1``, ``[::1]``,
    ``::ffff:127.x.x.x``. Names (``localhost``, ``testclient``), empty or None -> False."""
    ip = _parse_ip(client_host)
    if ip is None:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def client_host(request: Any) -> str | None:
    """The TCP peer address of a Starlette-like request (``request.client.host``), or None.
    Never reads headers, so it cannot be spoofed by X-Forwarded-For."""
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return host if isinstance(host, str) else None


def remote_enabled(settings: Any) -> bool:
    """``dashboard.remote_access`` is explicitly on (default off, fail safe)."""
    value = settings.get("dashboard.remote_access", False)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return value is True


def _request_path(request: Any) -> str:
    url = getattr(request, "url", None)
    path = getattr(url, "path", None)
    return path if isinstance(path, str) else ""


def _request_cookie(request: Any, name: str) -> str | None:
    cookies = getattr(request, "cookies", None) or {}
    try:
        value = cookies.get(name)
    except AttributeError:
        return None
    return value if isinstance(value, str) else None


def require_auth(request: Any, ctx: Any) -> str | None:
    """Gate for every request. None = let it through, else a reason code.

    Loopback clients always pass (no PIN on the PC). Remote clients need
    ``dashboard.remote_access: true`` (else ``"remote_disabled"``) and then either a public
    path (login page / login API / health) or a valid ``agency_session`` cookie (else
    ``"login_required"``). Host allow-list and CSRF checks stay in the app middleware.
    """
    if is_loopback(client_host(request)):
        return None
    if not remote_enabled(ctx.settings):
        return REASON_REMOTE_DISABLED
    if _request_path(request) in PUBLIC_PATHS:
        return None
    if validate_session(ctx.db, _request_cookie(request, COOKIE_NAME)):
        return None
    return REASON_LOGIN_REQUIRED


def denial(reason: str, path: str) -> tuple[int, dict, str | None]:
    """How to answer a :func:`require_auth` refusal: ``(status, json_body, redirect_to)``.

    A page request needing login gets ``(303, {}, "/login")``; API/static requests get JSON.
    """
    if reason == REASON_LOGIN_REQUIRED:
        is_page = not (path.startswith("/api/") or path.startswith("/static/"))
        if is_page and path != LOGIN_PATH:
            return 303, {}, LOGIN_PATH
        return 401, {"ok": False, "reason": reason, "detail": MSG_LOGIN_REQUIRED,
                     "login": LOGIN_PATH}, None
    if reason == REASON_REMOTE_DISABLED:
        return 403, {"ok": False, "reason": reason, "detail": MSG_REMOTE_DISABLED}, None
    return 403, {"ok": False, "reason": str(reason), "detail": MSG_LOGIN_REQUIRED}, None


# --------------------------------------------------------------------------- LAN / Host
def _is_lan_ipv4(value: Any) -> bool:
    ip = _parse_ip(value)
    return ip is not None and ip.version == 4 and any(ip in net for net in _LAN_NETWORKS)


def lan_addresses() -> list[str]:
    """This machine's private LAN IPv4 addresses, the default-route one first.

    No traffic: UDP ``connect()`` to private addresses only selects a route, and
    ``getaddrinfo(gethostname())`` resolves locally. Loopback / link-local / public dropped.
    """
    found: list[str] = []
    for target in _ROUTE_PROBES:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect((target, 9))
                found.append(str(sock.getsockname()[0]))
        except OSError:
            continue
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(str(info[4][0]))
    except (OSError, UnicodeError):
        pass
    out: list[str] = []
    for addr in found:
        if _is_lan_ipv4(addr) and addr not in out:
            out.append(addr)
    return out


def _hostnames() -> set[str]:
    try:
        name = socket.gethostname().strip().lower().rstrip(".")
    except OSError:
        return set()
    if not name or not _HOSTNAME_RE.match(name):
        return set()
    short = name.split(".", 1)[0]
    return {name, short, f"{short}.local"}


def allowed_hosts(port: int, remote_enabled: bool) -> set[str]:
    """Host header allow-list (DNS-rebinding defence): loopback names, plus this machine's LAN
    IPs and host name when remote access is on. Each name appears bare and as ``name:port``."""
    names = set(LOOPBACK_HOSTS)
    if remote_enabled:
        names |= set(lan_addresses())
        names |= _hostnames()
    out = set(names)
    port = int(port)
    for name in names:
        if name == "::1":
            continue  # a Host header carries IPv6 in brackets
        out.add(f"{name}:{port}")
    return out


def _split_host_header(host: str) -> tuple[str, str | None] | None:
    host = host.strip().lower()
    if not host:
        return None
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return None
        name, rest = host[:end + 1], host[end + 1:]
        if not rest:
            return name, None
        if not rest.startswith(":"):
            return None
        port = rest[1:]
    elif host.count(":") == 1:
        name, port = host.split(":", 1)
    elif ":" in host:
        return host, None  # bare IPv6 (not valid in a Host header; matched literally)
    else:
        return host.rstrip("."), None
    if not port.isdigit():
        return None
    return name.rstrip("."), port


def host_allowed(host_header: str | None, allowed: set[str]) -> bool:
    """True when the Host header (``name`` or ``name:port``) is in ``allowed``."""
    parts = _split_host_header(host_header or "")
    if parts is None:
        return False
    name, port = parts
    return (name if port is None else f"{name}:{port}") in allowed


def phone_urls(port: int) -> list[str]:
    """``http://<LAN-IP>:<port>/`` for each LAN address (best guess first) — for the QR."""
    return [f"http://{ip}:{int(port)}/" for ip in lan_addresses()]


# --------------------------------------------------------------------------- QR
def qr_svg(url: str, *, dark: str = "#0b0f14", light: str | None = "#ffffff", scale: int = 6,
           border: int = 4, label: str = "קוד QR לפתיחה בטלפון") -> str:
    """Inline ``<svg>`` QR code for ``url`` (segno, pure python). Size it with CSS.

    Dark modules on a light background scan most reliably; ``light=None`` is transparent.
    """
    if not isinstance(url, str) or not re.match(r"^https?://", url) or len(url) > 1000:
        raise ValueError("qr_svg needs an http(s) URL")
    try:
        import segno
    except ImportError as exc:  # pragma: no cover - segno is a core dependency
        raise RuntimeError("segno is not installed (pip install segno)") from exc
    qr = segno.make_qr(url, error="m")
    svg = qr.svg_inline(dark=dark, light=light, scale=scale, border=border, omitsize=True)
    return svg.replace("<svg ", f'<svg role="img" aria-label="{html.escape(label)}" ', 1)


# --------------------------------------------------------------------------- login page
def new_nonce() -> str:
    """A CSP nonce for one login page render."""
    return secrets.token_urlsafe(18)


def _check_nonce(nonce: str) -> str:
    if not isinstance(nonce, str) or not _NONCE_RE.match(nonce):
        raise ValueError("bad CSP nonce")
    return nonce


def render_login(nonce: str) -> str:
    """The standalone login page (inline CSS/JS tagged with ``nonce``)."""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)),
                      autoescape=select_autoescape(["html"]))
    return env.get_template("login.html").render(csp_nonce=_check_nonce(nonce))


def login_csp(nonce: str) -> str:
    """Content-Security-Policy for the login page: only its own nonce'd inline CSS/JS."""
    n = _check_nonce(nonce)
    return (f"default-src 'none'; script-src 'nonce-{n}'; style-src 'nonce-{n}'; "
            "connect-src 'self'; img-src data:; form-action 'self'; frame-ancestors 'none'; "
            "base-uri 'none'")


# --------------------------------------------------------------------------- summary
def status_summary(db: Any, settings: Any) -> dict:
    """For the settings screen: {"remote_enabled", "pin_set", "pin_created_at", "devices"}."""
    return {"remote_enabled": remote_enabled(settings), "pin_set": pin_is_set(db),
            "pin_created_at": pin_created_at(db), "devices": session_count(db)}
