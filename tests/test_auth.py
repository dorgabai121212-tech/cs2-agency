"""Phone access (SPEC_V2 §6): PIN hashing, rate limit + lockout, sessions, loopback checks,
Host allow-list, cookie flags, QR and the login page. Offline: LAN sockets are faked."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import re
import socket
import sqlite3
import threading
from types import SimpleNamespace
from typing import ClassVar

import pytest

# Module level on purpose: FastAPI resolves the string annotations of the mini app from globals.
from fastapi import Request

from agency.dashboard import auth

PIN = "482913"
WRONG = "111111"
IP = "192.168.1.20"
OTHER_IP = "192.168.1.21"
LAN_IP = "192.168.1.5"
PORT = 8765
DAY = 24 * 3600


# --------------------------------------------------------------------------- helpers
class Clock:
    """Stands in for ``auth._now``; ``advance`` moves time forward (or back)."""

    def __init__(self, start: float = 1_800_000_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(auth, "_now", c)
    return c


@pytest.fixture
def fast_scrypt(monkeypatch: pytest.MonkeyPatch):
    """Cheap scrypt cost for tests that hash many times (real cost: test_hash_pin_*)."""
    monkeypatch.setattr(auth, "SCRYPT_N", 2 ** 8)


class FakeSettings:
    def __init__(self, **raw):
        self.raw = raw

    def get(self, dotted: str, default=None):
        node = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node


def make_ctx(db, remote: object = True) -> SimpleNamespace:
    return SimpleNamespace(db=db, settings=FakeSettings(dashboard={"remote_access": remote}))


def make_request(host, path: str = "/", cookies: dict | None = None,
                 headers: dict | None = None) -> SimpleNamespace:
    client = SimpleNamespace(host=host, port=50000) if host is not None else None
    return SimpleNamespace(client=client, url=SimpleNamespace(path=path),
                           cookies=cookies or {}, headers=headers or {})


def starlette_request(host: str, path: str = "/", cookie: str | None = None,
                      headers: dict | None = None):
    from starlette.requests import Request

    raw = [(b"host", f"{LAN_IP}:{PORT}".encode())]
    raw += [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if cookie is not None:
        raw.append((b"cookie", f"{auth.COOKIE_NAME}={cookie}".encode()))
    return Request({"type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
                    "query_string": b"", "headers": raw, "client": (host, 50000),
                    "server": (LAN_IP, PORT), "scheme": "http", "root_path": ""})


def kv_raw_values(db) -> list[str]:
    con = sqlite3.connect(str(db.path))
    try:
        return [r[0] or "" for r in con.execute("SELECT value FROM kv")]
    finally:
        con.close()


def lock_out(db, ip: str = IP) -> auth.PinCheck:
    check = None
    for _ in range(auth.MAX_FAILURES):
        check = auth.verify_pin(db, WRONG, ip)
    assert check is not None and check.reason == auth.REASON_LOCKED
    return check


# --------------------------------------------------------------------------- PIN
def test_generate_pin_is_six_random_digits(monkeypatch: pytest.MonkeyPatch):
    pins = [auth.generate_pin() for _ in range(300)]
    assert all(re.fullmatch(r"[0-9]{6}", p) for p in pins)
    assert len(set(pins)) > 250  # random, not a sequence

    calls = []

    def fake_randbelow(n):
        calls.append(n)
        return 42

    monkeypatch.setattr(auth.secrets, "randbelow", fake_randbelow)
    assert auth.generate_pin() == "000042"  # zero-padded
    assert calls == [10 ** 6]


@pytest.mark.parametrize("raw, expected", [
    ("482913", "482913"),
    (" 482 913 ", "482913"),
    ("482-913", "482913"),
    ("٤٨٢٩١٣", "482913"),        # Arabic-Indic digits (phone keyboards)
    ("۴۸۲۹۱۳", "482913"),        # Persian digits
    ("４８２９１３", "482913"),   # full-width
    ("48291", None),
    ("4829133", None),
    ("48291a", None),
    ("", None),
    (None, None),
    (482913, None),
    ("4" * 40, None),
])
def test_normalize_pin(raw, expected):
    assert auth.normalize_pin(raw) == expected


def test_hash_pin_is_salted_scrypt_with_real_cost():
    rec = auth.hash_pin(PIN)
    assert set(rec) == {"algo", "salt", "hash", "n", "r", "p", "created_at"}
    assert rec["algo"] == "scrypt"
    assert (rec["n"], rec["r"], rec["p"]) == (2 ** 14, 8, 1)
    salt = bytes.fromhex(rec["salt"])
    assert len(salt) == 16
    expected = hashlib.scrypt(PIN.encode(), salt=salt, n=rec["n"], r=rec["r"], p=rec["p"],
                              dklen=32, maxmem=auth.SCRYPT_MAXMEM)
    assert bytes.fromhex(rec["hash"]) == expected

    again = auth.hash_pin(PIN)
    assert again["salt"] != rec["salt"] and again["hash"] != rec["hash"]  # salted
    plain = {k: v for k, v in rec.items() if k not in ("salt", "hash")}
    assert PIN not in json.dumps(plain)

    assert auth.pin_matches(PIN, rec)
    assert auth.pin_matches("482 913", rec)
    assert not auth.pin_matches("482914", rec)
    with pytest.raises(ValueError):
        auth.hash_pin("12345")


def test_pin_matches_uses_constant_time_compare(monkeypatch: pytest.MonkeyPatch, fast_scrypt):
    rec = auth.hash_pin(PIN)
    calls = []
    real = auth.hmac.compare_digest

    def spy(a, b):
        calls.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    assert auth.pin_matches(PIN, rec)
    assert not auth.pin_matches(WRONG, rec)
    assert calls == [(bytes, bytes), (bytes, bytes)]


def test_pin_matches_fails_closed_on_bad_records(monkeypatch: pytest.MonkeyPatch, fast_scrypt):
    good = auth.hash_pin(PIN)
    bad_records = [
        None, {}, "x", {**good, "algo": "md5"}, {**good, "n": 1000},
        {**good, "n": 2 ** 20, "r": 32},          # would need gigabytes of memory
        {**good, "salt": "zz"}, {**good, "hash": "ab"}, {**good, "p": 0},
        {k: v for k, v in good.items() if k != "salt"},
    ]
    for rec in bad_records:
        assert not auth.pin_matches(PIN, rec), rec

    def broken(*args, **kwargs):
        raise ValueError("memory limit exceeded")

    monkeypatch.setattr(auth, "_scrypt", broken)
    assert not auth.pin_matches(PIN, good)


def test_set_pin_stores_only_the_hash(db, monkeypatch: pytest.MonkeyPatch, clock):
    monkeypatch.setattr(auth.secrets, "token_bytes", lambda n: b"\x01" * n)  # deterministic
    auth.set_pin(db, PIN)
    rec = db.kv_get(auth.PIN_KEY)
    assert rec["algo"] == "scrypt" and auth.pin_matches(PIN, rec)
    for value in kv_raw_values(db):
        assert PIN not in value
    assert auth.pin_is_set(db)
    assert auth.pin_created_at(db).startswith("2027-01-15T")  # clock start, UTC ISO


def test_rotate_pin_replaces_old_pin_and_resets_limits(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    for _ in range(3):
        auth.verify_pin(db, WRONG, IP)
    new = auth.rotate_pin(db)
    assert re.fullmatch(r"[0-9]{6}", new)
    if new != PIN:
        check = auth.verify_pin(db, PIN, IP)
        assert check.reason == auth.REASON_BAD_PIN
        assert check.attempts_left == auth.MAX_FAILURES - 1  # old failures were reset
    assert auth.verify_pin(db, new, IP).ok
    assert all(new not in v for v in kv_raw_values(db))


def test_clear_pin(db, clock, fast_scrypt):
    assert not auth.pin_is_set(db)
    assert auth.pin_created_at(db) is None
    auth.rotate_pin(db)
    assert auth.pin_is_set(db)
    auth.clear_pin(db)
    assert not auth.pin_is_set(db)
    assert auth.verify_pin(db, PIN, IP).reason == auth.REASON_NO_PIN


# --------------------------------------------------------------------------- rate limiting
def test_wrong_pins_count_down_then_lock(db, clock, fast_scrypt, monkeypatch):
    auth.set_pin(db, PIN)
    for left in (4, 3, 2, 1):
        check = auth.verify_pin(db, WRONG, IP)
        assert (check.ok, check.reason, check.attempts_left) == (False, auth.REASON_BAD_PIN, left)
    check = auth.verify_pin(db, WRONG, IP)
    assert (check.ok, check.reason, check.attempts_left) == (False, auth.REASON_LOCKED, 0)
    assert check.retry_after_s == 600 and check.locked_minutes == 10
    assert auth.lockout_remaining(db, IP) == 600

    def no_hashing(*args, **kwargs):
        raise AssertionError("a locked client must not even be hashed")

    monkeypatch.setattr(auth, "_scrypt", no_hashing)
    check = auth.verify_pin(db, PIN, IP)  # even the right PIN is refused while locked
    assert (check.ok, check.reason) == (False, auth.REASON_LOCKED)


def test_lockout_expires_when_the_window_passes(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    lock_out(db)
    clock.advance(599)
    check = auth.verify_pin(db, PIN, IP)
    assert check.reason == auth.REASON_LOCKED
    assert check.retry_after_s == 1 and check.locked_minutes == 1
    clock.advance(1)
    assert auth.lockout_remaining(db, IP) == 0
    assert auth.verify_pin(db, PIN, IP).ok
    # a finished lockout is a clean slate
    assert auth.verify_pin(db, WRONG, IP).attempts_left == 4


def test_lockout_is_per_ip(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    lock_out(db, IP)
    assert auth.lockout_remaining(db, OTHER_IP) == 0
    assert auth.verify_pin(db, PIN, OTHER_IP).ok
    assert auth.verify_pin(db, PIN, IP).reason == auth.REASON_LOCKED


def test_lockout_survives_a_restart(settings, clock, fast_scrypt):
    from agency.db import DB

    first = DB(settings.db_path)
    auth.set_pin(first, PIN)
    lock_out(first)
    second = DB(settings.db_path)  # new process / dashboard restart
    assert auth.verify_pin(second, PIN, IP).reason == auth.REASON_LOCKED
    assert auth.lockout_remaining(second, IP) == 600


def test_failures_outside_the_window_are_forgotten(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    for _ in range(4):
        auth.verify_pin(db, WRONG, IP)
    clock.advance(auth.WINDOW_S + 1)
    for left in (4, 3, 2, 1):
        assert auth.verify_pin(db, WRONG, IP).attempts_left == left


def test_failures_inside_the_window_add_up(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    for _ in range(4):
        auth.verify_pin(db, WRONG, IP)
        clock.advance(120)  # 4 failures spread over 8 minutes
    check = auth.verify_pin(db, WRONG, IP)
    assert check.reason == auth.REASON_LOCKED and check.retry_after_s == auth.LOCKOUT_S


def test_success_clears_the_ip_failures(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    for _ in range(4):
        auth.verify_pin(db, WRONG, IP)
    assert auth.verify_pin(db, PIN, IP).ok
    assert auth.verify_pin(db, WRONG, IP).attempts_left == 4


def test_malformed_pin_is_refused_without_counting(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    for junk in ("12ab", "", "1234567", None, {"pin": PIN}) * 3:
        check = auth.verify_pin(db, junk, IP)
        assert (check.reason, check.attempts_left) == (auth.REASON_INVALID_FORMAT, 5)
    assert auth.verify_pin(db, WRONG, IP).attempts_left == 4


def test_no_pin_set_refuses_everything(db, clock):
    check = auth.verify_pin(db, PIN, IP)
    assert (check.ok, check.reason) == (False, auth.REASON_NO_PIN)


@pytest.mark.parametrize("raw, key", [
    ("192.168.1.20", "192.168.1.20"),
    ("::ffff:192.168.1.20", "192.168.1.20"),        # IPv4-mapped shares the IPv4 bucket
    ("2001:db8:1:2::abcd", "2001:db8:1:2::/64"),
    ("[2001:db8:1:2::1]", "2001:db8:1:2::/64"),
    ("fe80::1%eth0", "fe80::/64"),
    (None, "unknown"),
    ("", "unknown"),
    ("testclient", "other:testclient"),
])
def test_ip_key(raw, key):
    assert auth.ip_key(raw) == key


def test_ipv6_clients_are_limited_per_slash_64(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    checks = [auth.verify_pin(db, WRONG, f"2001:db8:1:2::{i}") for i in range(1, 6)]
    assert checks[-1].reason == auth.REASON_LOCKED  # address hopping inside the /64 is no escape
    assert auth.verify_pin(db, PIN, "2001:db8:1:2::99").reason == auth.REASON_LOCKED
    assert auth.verify_pin(db, PIN, "2001:db8:1:3::1").ok


def test_global_limit_locks_every_ip(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    checks = [auth.verify_pin(db, WRONG, f"10.0.0.{i}")
              for i in range(1, auth.GLOBAL_MAX_FAILURES + 1)]
    assert all(c.reason == auth.REASON_BAD_PIN for c in checks[:-1])
    assert checks[-1].reason == auth.REASON_LOCKED
    assert auth.verify_pin(db, PIN, "10.0.0.250").reason == auth.REASON_LOCKED
    clock.advance(auth.LOCKOUT_S)
    assert auth.verify_pin(db, PIN, "10.0.0.250").ok


def test_clock_going_backwards_never_extends_a_lock(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    lock_out(db)
    clock.advance(-3600)  # Windows clock corrected one hour back
    check = auth.verify_pin(db, PIN, IP)
    assert check.reason == auth.REASON_LOCKED and 0 < check.retry_after_s <= auth.LOCKOUT_S
    clock.advance(auth.LOCKOUT_S)
    assert auth.verify_pin(db, PIN, IP).ok


def test_future_failure_stamps_still_count(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    for _ in range(4):
        auth.verify_pin(db, WRONG, IP)
    clock.advance(-60)  # clock moved back: earlier failures look like the future
    assert auth.verify_pin(db, WRONG, IP).reason == auth.REASON_LOCKED


@pytest.mark.parametrize("state", [
    "garbage", 42, [], {"ips": "x", "global": 5},
    {"ips": {IP: {"fails": "abc", "locked_until": "soon"}}, "global": {"fails": [None]}},
    {"ips": {IP: {"fails": [float("nan")], "locked_until": float("inf")}}},
])
def test_corrupt_limit_state_is_tolerated(db, clock, fast_scrypt, state):
    auth.set_pin(db, PIN)
    db.kv_set(auth.LIMITS_KEY, state)
    assert auth.verify_pin(db, WRONG, IP).reason == auth.REASON_BAD_PIN
    assert auth.verify_pin(db, PIN, IP).ok


def test_concurrent_wrong_pins_are_counted_exactly(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    barrier = threading.Barrier(8)
    results: list[auth.PinCheck] = []

    def attempt():
        barrier.wait()
        results.append(auth.verify_pin(db, WRONG, IP))

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    reasons = sorted(r.reason for r in results)
    assert reasons.count(auth.REASON_BAD_PIN) == 4
    assert reasons.count(auth.REASON_LOCKED) == 4


def test_limits_are_kept_in_the_kv_table(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    auth.verify_pin(db, WRONG, IP)
    state = db.kv_get(auth.LIMITS_KEY)
    assert state["ips"][IP]["fails"] == [clock.t]
    assert state["ips"][IP]["locked_until"] == 0


@pytest.mark.parametrize("seconds, minutes", [(600, 10), (601, 11), (61, 2), (60, 1), (1, 1),
                                              (0, 0)])
def test_locked_minutes_rounds_up(seconds, minutes):
    assert auth.PinCheck(False, auth.REASON_LOCKED, 0, seconds).locked_minutes == minutes


def test_messages_are_short_friendly_hebrew():
    locked = auth.check_message(auth.PinCheck(False, auth.REASON_LOCKED, 0, 600))
    assert locked == "יותר מדי ניסיונות. אפשר לנסות שוב בעוד 10 דקות."
    assert "בעוד דקה" in auth.check_message(auth.PinCheck(False, auth.REASON_LOCKED, 0, 30))
    assert "ניסיון אחד" in auth.check_message(auth.PinCheck(False, auth.REASON_BAD_PIN, 1))
    assert "3 ניסיונות" in auth.check_message(auth.PinCheck(False, auth.REASON_BAD_PIN, 3))
    texts = [auth.check_message(auth.PinCheck(False, r, 2, 120)) for r in
             (auth.REASON_LOCKED, auth.REASON_BAD_PIN, auth.REASON_NO_PIN,
              auth.REASON_INVALID_FORMAT)]
    texts += [auth.MSG_REMOTE_DISABLED, auth.MSG_LOGIN_REQUIRED, auth.MSG_LOCAL_ONLY]
    for text in texts:
        assert text and re.search(r"[א-ת]", text)
        assert not re.search(r"(?i)pin|token|api|session|cookie|json", text)


# --------------------------------------------------------------------------- login()
def test_login_success_returns_token_only_for_the_cookie(db, clock, fast_scrypt):
    auth.set_pin(db, PIN)
    out = auth.login(db, PIN, IP, label="iPhone Safari")
    assert out.status == 200 and out.body == {"ok": True} and out.headers == {}
    assert isinstance(out.token, str) and auth.validate_session(db, out.token)
    dumped = json.dumps(out.body)
    assert PIN not in dumped and out.token not in dumped


def test_login_failure_shapes(db, clock, fast_scrypt):
    no_pin = auth.login(db, PIN, IP)
    assert no_pin.status == 403 and no_pin.token is None
    assert no_pin.body == {"ok": False, "reason": "no_pin", "detail": auth.MSG_NO_PIN}

    auth.set_pin(db, PIN)
    bad_format = auth.login(db, "12", IP)
    assert bad_format.status == 400
    assert bad_format.body["reason"] == "invalid_format" and bad_format.body["attempts_left"] == 5

    bad = auth.login(db, WRONG, IP)
    assert bad.status == 401 and bad.token is None
    assert set(bad.body) == {"ok", "reason", "detail", "attempts_left"}
    assert bad.body["reason"] == "bad_pin" and bad.body["attempts_left"] == 4

    for _ in range(3):
        auth.login(db, WRONG, IP)
    locked = auth.login(db, WRONG, IP)
    assert locked.status == 429 and locked.token is None
    assert locked.headers == {"Retry-After": "600"}
    assert locked.body == {"ok": False, "reason": "locked",
                           "detail": "יותר מדי ניסיונות. אפשר לנסות שוב בעוד 10 דקות.",
                           "retry_after_s": 600, "locked_minutes": 10}
    assert auth.login(db, PIN, IP).status == 429


# --------------------------------------------------------------------------- sessions
def test_session_token_is_random_and_stored_hashed(db, clock, monkeypatch):
    sizes = []
    real = auth.secrets.token_urlsafe

    def spy(n):
        sizes.append(n)
        return real(n)

    monkeypatch.setattr(auth.secrets, "token_urlsafe", spy)
    token = auth.create_session(db)
    assert sizes == [32]
    assert re.fullmatch(r"[A-Za-z0-9_\-]{43}", token)
    stored = db.kv_get(auth.SESSIONS_KEY)
    assert list(stored) == [hashlib.sha256(token.encode()).hexdigest()]
    meta = stored[hashlib.sha256(token.encode()).hexdigest()]
    assert meta["expires"] - meta["created"] == 30 * DAY
    assert all(token not in v for v in kv_raw_values(db))
    assert auth.validate_session(db, token)
    assert auth.create_session(db) != token


@pytest.mark.parametrize("junk", [None, "", 123, "short", "x" * 500, "ש" * 43,
                                  "A" * 43, b"bytes-token-bytes-token-bytes"])
def test_validate_session_rejects_junk(db, clock, junk):
    auth.create_session(db)
    assert auth.validate_session(db, junk) is False


def test_session_expires_after_30_days(db, clock):
    token = auth.create_session(db)
    clock.advance(30 * DAY - 1)
    assert auth.validate_session(db, token)
    clock.advance(1)
    assert not auth.validate_session(db, token)
    assert auth.session_count(db) == 0


def test_revoke_one_and_revoke_all(db, clock):
    t1, t2, t3 = (auth.create_session(db) for _ in range(3))
    assert auth.revoke_session(db, t1) is True
    assert auth.revoke_session(db, t1) is False
    assert not auth.validate_session(db, t1)
    assert auth.validate_session(db, t2) and auth.validate_session(db, t3)
    assert auth.revoke_all(db) == 2
    assert not auth.validate_session(db, t2) and not auth.validate_session(db, t3)
    assert auth.session_count(db) == 0
    assert auth.revoke_all(db) == 0
    assert auth.revoke_session(db, None) is False


def test_session_store_keeps_the_newest(db, clock):
    tokens = []
    for _ in range(auth.MAX_SESSIONS + 1):
        tokens.append(auth.create_session(db))
        clock.advance(1)
    assert auth.session_count(db) == auth.MAX_SESSIONS
    assert not auth.validate_session(db, tokens[0])
    assert all(auth.validate_session(db, t) for t in tokens[1:])


def test_expired_sessions_are_pruned_on_create(db, clock):
    old = auth.create_session(db)
    clock.advance(31 * DAY)
    auth.create_session(db)
    assert hashlib.sha256(old.encode()).hexdigest() not in db.kv_get(auth.SESSIONS_KEY)


def test_session_label_is_cleaned(db, clock):
    auth.create_session(db, label="iPhone\x00\x1b[31m" + "x" * 500)
    (meta,) = db.kv_get(auth.SESSIONS_KEY).values()
    assert meta["label"].startswith("iPhone[31m") and len(meta["label"]) <= 120
    assert all(ch.isprintable() for ch in meta["label"])


def test_works_with_a_plain_duck_typed_kv_store(clock, fast_scrypt):
    class MemoryKV:
        def __init__(self):
            self.data = {}

        def kv_get(self, key, default=None):
            return json.loads(self.data[key]) if key in self.data else default

        def kv_set(self, key, value):
            self.data[key] = json.dumps(value)

    store = MemoryKV()
    auth.set_pin(store, PIN)
    assert auth.verify_pin(store, WRONG, IP).attempts_left == 4
    out = auth.login(store, PIN, IP)
    assert out.status == 200 and auth.validate_session(store, out.token)
    assert auth.revoke_all(store) == 1


# --------------------------------------------------------------------------- loopback
@pytest.mark.parametrize("host", ["127.0.0.1", "127.8.9.10", "::1", "[::1]", "::ffff:127.0.0.1",
                                  "0:0:0:0:0:0:0:1", " 127.0.0.1 "])
def test_is_loopback_true(host):
    assert auth.is_loopback(host) is True


@pytest.mark.parametrize("host", ["localhost", "testclient", "", None, IP, "::ffff:192.168.1.20",
                                  "0.0.0.0", "::", "fe80::1", "127.0.0.1.nip.io",
                                  "127.0.0.1:8765", "::127.0.0.1", 2130706433, b"127.0.0.1"])
def test_is_loopback_false(host):
    assert auth.is_loopback(host) is False


def test_client_host_never_reads_forwarding_headers(db, clock):
    spoof = {"x-forwarded-for": "127.0.0.1", "x-real-ip": "127.0.0.1",
             "forwarded": "for=127.0.0.1", "host": "127.0.0.1:8765"}
    fake = make_request(IP, headers=spoof)
    assert auth.client_host(fake) == IP
    assert auth.require_auth(fake, make_ctx(db)) == auth.REASON_LOGIN_REQUIRED
    real = starlette_request(IP, headers=spoof)
    assert auth.client_host(real) == IP
    assert auth.require_auth(real, make_ctx(db)) == auth.REASON_LOGIN_REQUIRED
    assert auth.client_host(SimpleNamespace(client=None)) is None
    assert auth.client_host(object()) is None


# --------------------------------------------------------------------------- require_auth
@pytest.mark.parametrize("remote", [True, False])
@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_require_auth_loopback_needs_no_pin(db, host, remote):
    for path in ("/", "/api/home", "/api/settings", "/login"):
        assert auth.require_auth(make_request(host, path), make_ctx(db, remote)) is None


def test_require_auth_remote_access_off_refuses_everything(db, clock):
    token = auth.create_session(db)
    ctx = make_ctx(db, remote=False)
    for path in ("/", "/login", "/api/auth/login", "/api/health"):
        req = make_request(IP, path, cookies={auth.COOKIE_NAME: token})
        assert auth.require_auth(req, ctx) == auth.REASON_REMOTE_DISABLED


def test_require_auth_remote_needs_a_valid_cookie(db, clock):
    ctx = make_ctx(db, remote=True)
    assert auth.require_auth(make_request(IP, "/"), ctx) == auth.REASON_LOGIN_REQUIRED
    assert auth.require_auth(make_request(IP, "/api/home"), ctx) == auth.REASON_LOGIN_REQUIRED
    junk = make_request(IP, "/", cookies={auth.COOKIE_NAME: "A" * 43})
    assert auth.require_auth(junk, ctx) == auth.REASON_LOGIN_REQUIRED
    wrong_name = make_request(IP, "/", cookies={"agency_key": auth.create_session(db)})
    assert auth.require_auth(wrong_name, ctx) == auth.REASON_LOGIN_REQUIRED

    for path in sorted(auth.PUBLIC_PATHS):
        assert auth.require_auth(make_request(IP, path), ctx) is None
    for path in ("/api/auth/login/", "/login/", "/LOGIN", "/api/health/x"):
        assert auth.require_auth(make_request(IP, path), ctx) == auth.REASON_LOGIN_REQUIRED

    token = auth.create_session(db)
    ok = make_request(IP, "/api/approvals", cookies={auth.COOKIE_NAME: token})
    assert auth.require_auth(ok, ctx) is None
    assert auth.require_auth(starlette_request(IP, "/api/home", cookie=token), ctx) is None

    clock.advance(30 * DAY)
    assert auth.require_auth(ok, ctx) == auth.REASON_LOGIN_REQUIRED


def test_require_auth_after_revoke_all(db, clock):
    ctx = make_ctx(db, remote=True)
    token = auth.create_session(db)
    req = make_request(IP, "/", cookies={auth.COOKIE_NAME: token})
    assert auth.require_auth(req, ctx) is None
    auth.revoke_all(db)
    assert auth.require_auth(req, ctx) == auth.REASON_LOGIN_REQUIRED


def test_require_auth_without_a_peer_address_is_remote(db):
    assert auth.require_auth(make_request(None), make_ctx(db, True)) == auth.REASON_LOGIN_REQUIRED
    assert auth.require_auth(make_request(None), make_ctx(db, False)) == \
        auth.REASON_REMOTE_DISABLED


@pytest.mark.parametrize("value, expected", [
    (True, True), (False, False), ("true", True), (" On ", True), ("1", True), ("yes", True),
    ("no", False), ("false", False), ("", False), (None, False), (1, False), ([], False),
])
def test_remote_enabled_is_explicit_opt_in(value, expected):
    assert auth.remote_enabled(FakeSettings(dashboard={"remote_access": value})) is expected


def test_remote_enabled_defaults_off(settings):
    assert auth.remote_enabled(FakeSettings()) is False
    assert auth.remote_enabled(settings) is False  # config.example.yaml ships it off


def test_require_local(db, clock):
    token = auth.create_session(db)
    assert auth.require_local(make_request("127.0.0.1")) is None
    assert auth.require_local(make_request("::1")) is None
    phone = make_request(IP, cookies={auth.COOKIE_NAME: token})
    assert auth.require_local(phone) == auth.REASON_LOCAL_ONLY
    assert auth.require_local(make_request(None)) == auth.REASON_LOCAL_ONLY


def test_denial_shapes():
    assert auth.denial(auth.REASON_LOGIN_REQUIRED, "/") == (303, {}, "/login")
    assert auth.denial(auth.REASON_LOGIN_REQUIRED, "/settings") == (303, {}, "/login")
    for path in ("/api/home", "/static/app.js", "/login"):  # no redirect loop on /login
        status, body, redirect = auth.denial(auth.REASON_LOGIN_REQUIRED, path)
        assert (status, redirect) == (401, None)
        assert body == {"ok": False, "reason": "login_required",
                        "detail": auth.MSG_LOGIN_REQUIRED, "login": "/login"}
    assert auth.denial(auth.REASON_REMOTE_DISABLED, "/") == (
        403, {"ok": False, "reason": "remote_disabled", "detail": auth.MSG_REMOTE_DISABLED}, None)
    assert auth.denial(auth.REASON_LOCAL_ONLY, "/api/auth/pairing") == (
        403, {"ok": False, "reason": "local_only", "detail": auth.MSG_LOCAL_ONLY}, None)
    assert auth.denial("other", "/")[0] == 403


# --------------------------------------------------------------------------- cookie
def test_cookie_kwargs_exact():
    assert auth.COOKIE_NAME == "agency_session"
    assert auth.COOKIE_KWARGS == {"max_age": 30 * DAY, "path": "/", "secure": False,
                                  "httponly": True, "samesite": "strict"}


def test_cookie_header_flags_via_starlette():
    from starlette.responses import Response

    resp = Response()
    resp.set_cookie(auth.COOKIE_NAME, "tok", **auth.COOKIE_KWARGS)
    header = resp.headers["set-cookie"]
    assert header.startswith("agency_session=tok;")
    assert "HttpOnly" in header and "Path=/" in header and "Max-Age=2592000" in header
    assert re.search(r"(?i)samesite=strict", header)
    assert "secure" not in header.lower()

    gone = Response()
    gone.delete_cookie(auth.COOKIE_NAME, **auth.COOKIE_DELETE_KWARGS)
    assert "Max-Age=0" in gone.headers["set-cookie"]


# --------------------------------------------------------------------------- LAN addresses
class FakeUDPSocket:
    """Records route probes; any attempt to actually send fails the test."""

    connected: ClassVar[list[tuple[str, int]]] = []
    local_ip: ClassVar[str] = LAN_IP
    fail: ClassVar[bool] = False

    def __init__(self, family=socket.AF_INET, kind=socket.SOCK_DGRAM, *args):
        assert family == socket.AF_INET and kind == socket.SOCK_DGRAM

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, addr):
        if FakeUDPSocket.fail:
            raise OSError("network unreachable")
        FakeUDPSocket.connected.append(addr)

    def getsockname(self):
        return (FakeUDPSocket.local_ip, 54321)

    def send(self, *args):  # pragma: no cover - must never run
        raise AssertionError("lan_addresses must not send packets")

    sendto = sendall = send


@pytest.fixture
def fake_net(monkeypatch: pytest.MonkeyPatch):
    FakeUDPSocket.connected = []
    FakeUDPSocket.local_ip = LAN_IP
    FakeUDPSocket.fail = False
    monkeypatch.setattr(auth.socket, "socket", FakeUDPSocket)
    monkeypatch.setattr(auth.socket, "gethostname", lambda: "DESKTOP-4P10HRU")
    infos = ["127.0.1.1", "169.254.3.4", "8.8.8.8", "10.0.0.7", LAN_IP, "172.20.0.1",
             "172.32.0.1", "100.64.1.1"]

    def fake_getaddrinfo(host, port, family=0, *args):
        assert host == "DESKTOP-4P10HRU" and family == socket.AF_INET
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in infos]

    monkeypatch.setattr(auth.socket, "getaddrinfo", fake_getaddrinfo)
    return FakeUDPSocket


def test_lan_addresses_private_only_route_first_no_traffic(fake_net):
    assert auth.lan_addresses() == [LAN_IP, "10.0.0.7", "172.20.0.1"]
    assert fake_net.connected  # route probes happened...
    for host, _port in fake_net.connected:  # ...only towards private, non-internet addresses
        addr = ipaddress.ip_address(host)
        assert addr.is_private and not addr.is_global


def test_lan_addresses_offline(fake_net, monkeypatch: pytest.MonkeyPatch):
    fake_net.fail = True

    def no_dns(*args, **kwargs):
        raise socket.gaierror("no name")

    monkeypatch.setattr(auth.socket, "getaddrinfo", no_dns)
    assert auth.lan_addresses() == []


def test_lan_addresses_real_machine_is_safe():
    found = auth.lan_addresses()  # real sockets: UDP connect only, nothing is sent
    assert isinstance(found, list)
    for ip in found:
        assert ipaddress.ip_address(ip).is_private


def test_phone_urls(fake_net):
    assert auth.phone_urls(PORT)[0] == f"http://{LAN_IP}:{PORT}/"
    assert all(u.startswith("http://") and u.endswith(":8765/") for u in auth.phone_urls(PORT))


# --------------------------------------------------------------------------- Host allow-list
LOOPBACK_ALLOWED = {"127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.1:8765", "localhost:8765",
                    "[::1]:8765"}


def test_allowed_hosts_loopback_only_when_remote_off(monkeypatch: pytest.MonkeyPatch):
    def boom():
        raise AssertionError("LAN lookup not needed when phone access is off")

    monkeypatch.setattr(auth, "lan_addresses", boom)
    assert auth.allowed_hosts(PORT, False) == LOOPBACK_ALLOWED


def test_allowed_hosts_adds_lan_ips_and_host_name(fake_net):
    allowed = auth.allowed_hosts(PORT, True)
    assert LOOPBACK_ALLOWED <= allowed
    for name in (LAN_IP, "10.0.0.7", "desktop-4p10hru", "desktop-4p10hru.local"):
        assert name in allowed and f"{name}:{PORT}" in allowed
    assert "::1:8765" not in allowed
    assert "8.8.8.8" not in allowed and "169.254.3.4" not in allowed
    assert "0.0.0.0" not in allowed and "0.0.0.0:8765" not in allowed


def test_allowed_hosts_skips_odd_host_names(fake_net, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth.socket, "gethostname", lambda: "bad host!")
    monkeypatch.setattr(auth.socket, "getaddrinfo", lambda *a, **k: [])
    assert auth.allowed_hosts(PORT, True) == LOOPBACK_ALLOWED | {LAN_IP, f"{LAN_IP}:{PORT}"}

    def no_name():
        raise OSError("no hostname")

    monkeypatch.setattr(auth.socket, "gethostname", no_name)
    assert auth.allowed_hosts(PORT, True) == LOOPBACK_ALLOWED | {LAN_IP, f"{LAN_IP}:{PORT}"}


@pytest.mark.parametrize("header, remote, ok", [
    ("127.0.0.1:8765", False, True),
    ("localhost", False, True),
    ("LOCALHOST:8765", False, True),
    ("localhost.:8765", False, True),
    ("[::1]:8765", False, True),
    ("[::1]", False, True),
    ("192.168.1.5:8765", False, False),     # phone access off -> LAN names refused
    ("192.168.1.5:8765", True, True),
    ("desktop-4p10hru.local:8765", True, True),
    ("evil.example", True, False),          # DNS rebinding
    ("evil.example:8765", True, False),
    ("127.0.0.1.nip.io:8765", True, False),
    ("127.0.0.1:9999", True, False),        # other port
    ("127.0.0.1:", True, False),
    ("127.0.0.1:80a", True, False),
    ("[::1", True, False),
    ("[::1]x", True, False),
    ("evil@127.0.0.1", True, False),
    ("", True, False),
    (None, True, False),
])
def test_host_allowed(fake_net, header, remote, ok):
    assert auth.host_allowed(header, auth.allowed_hosts(PORT, remote)) is ok


# --------------------------------------------------------------------------- QR
def test_qr_svg_is_inline_svg_from_segno():
    import segno

    url = f"http://{LAN_IP}:{PORT}/"
    svg = auth.qr_svg(url)
    assert svg.startswith('<svg role="img" aria-label="')
    assert "viewBox=" in svg and "<path" in svg
    assert "<?xml" not in svg and "<script" not in svg.lower() and "href" not in svg
    assert not re.search(r"<svg[^>]*\s(width|height)=", svg)  # sized by CSS
    assert svg == auth.qr_svg(url)  # deterministic
    assert svg != auth.qr_svg(f"http://10.0.0.7:{PORT}/")
    expected = segno.make_qr(url, error="m").svg_inline(dark="#0b0f14", light="#ffffff", scale=6,
                                                        border=4, omitsize=True)
    assert svg.split(">", 1)[1] == expected.split(">", 1)[1]  # same QR matrix as segno


@pytest.mark.parametrize("bad", ["", "javascript:alert(1)", "ftp://x", "192.168.1.5:8765",
                                 None, 42, "http://" + "a" * 2000])
def test_qr_svg_rejects_non_http(bad):
    with pytest.raises(ValueError):
        auth.qr_svg(bad)


def test_qr_svg_escapes_label():
    svg = auth.qr_svg("http://192.168.1.5:8765/", label='"><script>x</script>')
    assert "<script>" not in svg and "&quot;&gt;&lt;script&gt;" in svg


def test_start_pairing(db, clock, fast_scrypt, fake_net):
    info = auth.start_pairing(db, PORT)
    assert set(info) == {"pin", "url", "urls", "qr_svg"}
    assert re.fullmatch(r"[0-9]{6}", info["pin"])
    assert info["url"] == f"http://{LAN_IP}:{PORT}/" and info["urls"][0] == info["url"]
    assert info["qr_svg"].startswith("<svg")
    assert all(info["pin"] not in v for v in kv_raw_values(db))
    assert auth.verify_pin(db, info["pin"], IP).ok


def test_start_pairing_without_lan(db, clock, fast_scrypt, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(auth, "lan_addresses", list)  # no LAN address at all
    info = auth.start_pairing(db, PORT)
    assert info["url"] is None and info["qr_svg"] is None and info["urls"] == []
    assert auth.pin_is_set(db)


# --------------------------------------------------------------------------- login page
def test_render_login_page():
    nonce = auth.new_nonce()
    page = auth.render_login(nonce)
    assert '<html lang="he" dir="rtl">' in page
    assert page.count(f'nonce="{nonce}"') == 2  # the one <style> and the one <script>
    assert '"/api/auth/login"' in page and '"X-Agency-Request": "1"' in page
    assert 'method: "POST"' in page and "JSON.stringify({ pin: pin })" in page
    assert 'inputmode="numeric"' in page and 'autocomplete="one-time-code"' in page
    assert "--bg: #0b0f14" in page and "--orange: #f5a623" in page  # dashboard palette
    assert "retry_after_s" in page and "דקות" in page  # lockout minutes shown
    # no external assets at all
    assert "http://" not in page and "https://" not in page and "//cdn" not in page
    assert not re.search(r"<(link|script|img)[^>]+(src|href)=\"(?!data:)", page)
    assert "@import" not in page and "url(" not in page


def test_login_page_visible_text_has_no_jargon():
    page = auth.render_login(auth.new_nonce())
    visible = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", page)
    visible = re.sub(r"<[^>]+>", " ", visible)
    assert re.search(r"[א-ת]", visible)
    for word in ("dry-run", "api", "token", "secret", "oauth", "webhook", "json", "config",
                 "yaml", "pipeline", "pin", "cookie", "session"):
        assert word not in visible.lower(), word


def test_render_login_rejects_bad_nonce():
    for bad in ('x" onload="alert(1)', "", "short", None):
        with pytest.raises(ValueError):
            auth.render_login(bad)
        with pytest.raises(ValueError):
            auth.login_csp(bad)


def test_login_csp():
    nonce = auth.new_nonce()
    csp = auth.login_csp(nonce)
    assert "default-src 'none'" in csp
    assert f"script-src 'nonce-{nonce}'" in csp and f"style-src 'nonce-{nonce}'" in csp
    assert "connect-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-inline" not in csp


# --------------------------------------------------------------------------- summary / hygiene
def test_status_summary(db, clock, fast_scrypt):
    assert auth.status_summary(db, FakeSettings()) == {
        "remote_enabled": False, "pin_set": False, "pin_created_at": None, "devices": 0}
    auth.rotate_pin(db)
    auth.create_session(db)
    summary = auth.status_summary(db, FakeSettings(dashboard={"remote_access": True}))
    assert summary["remote_enabled"] is True and summary["pin_set"] is True
    assert summary["devices"] == 1 and summary["pin_created_at"].endswith("+00:00")


def test_nothing_secret_is_logged_or_printed(db, clock, fast_scrypt, caplog, capsys):
    caplog.set_level(logging.DEBUG)
    pin = auth.rotate_pin(db)
    auth.verify_pin(db, WRONG if pin != WRONG else "222222", IP)
    out = auth.login(db, pin, IP)
    captured = capsys.readouterr()
    for secret_value in (pin, out.token):
        assert secret_value not in caplog.text
        assert secret_value not in captured.out and secret_value not in captured.err


# --------------------------------------------------------------------------- wiring example
def _mini_app(ctx):
    """How the dashboard is expected to wire the module (mirrors app.py's guard)."""
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    app = FastAPI()
    allowed = auth.allowed_hosts(PORT, auth.remote_enabled(ctx.settings))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if not auth.host_allowed(request.headers.get("host"), allowed):
            return JSONResponse({"detail": "host not allowed"}, status_code=403)
        if request.method == "POST" and request.headers.get("x-agency-request") != "1":
            return JSONResponse({"detail": "missing X-Agency-Request header"}, status_code=403)
        reason = auth.require_auth(request, ctx)
        if reason:
            status, body, redirect = auth.denial(reason, request.url.path)
            if redirect:
                return RedirectResponse(redirect, status_code=status)
            return JSONResponse(body, status_code=status)
        return await call_next(request)

    @app.get("/")
    def home():
        return {"home": True}

    @app.get("/login")
    def login_page():
        nonce = auth.new_nonce()
        return HTMLResponse(auth.render_login(nonce),
                            headers={"Content-Security-Policy": auth.login_csp(nonce)})

    @app.post("/api/auth/login")
    async def do_login(request: Request):
        data = await request.json()
        out = auth.login(ctx.db, data.get("pin"), auth.client_host(request))
        resp = JSONResponse(out.body, status_code=out.status, headers=out.headers)
        if out.token:
            resp.set_cookie(auth.COOKIE_NAME, out.token, **auth.COOKIE_KWARGS)
        return resp

    return app


def test_end_to_end_phone_login(db, clock, fast_scrypt, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(auth, "lan_addresses", lambda: [LAN_IP])
    monkeypatch.setattr(auth.socket, "gethostname", lambda: "desktop-4p10hru")
    pin = auth.rotate_pin(db)
    app = _mini_app(make_ctx(db, remote=True))
    hdr = {"X-Agency-Request": "1"}

    with TestClient(app, base_url=f"http://{LAN_IP}:{PORT}", client=(IP, 50000),
                    follow_redirects=False) as phone:
        first = phone.get("/")
        assert first.status_code == 303 and first.headers["location"] == "/login"
        assert phone.get("/api/home").status_code == 401
        spoofed = phone.get("/", headers={"X-Forwarded-For": "127.0.0.1"})
        assert spoofed.status_code == 303  # forwarding headers are never trusted

        page = phone.get("/login")
        assert page.status_code == 200 and "default-src 'none'" in page.headers[
            "content-security-policy"]
        assert phone.post("/api/auth/login", json={"pin": pin}).status_code == 403  # no CSRF hdr
        wrong = phone.post("/api/auth/login", json={"pin": "000000" if pin != "000000"
                                                   else "999999"}, headers=hdr)
        assert wrong.status_code == 401 and wrong.json()["attempts_left"] == 4
        assert "set-cookie" not in wrong.headers

        ok = phone.post("/api/auth/login", json={"pin": pin}, headers=hdr)
        assert ok.status_code == 200 and ok.json() == {"ok": True}
        cookie = ok.headers["set-cookie"]
        assert "HttpOnly" in cookie and re.search(r"(?i)samesite=strict", cookie)
        assert pin not in ok.text
        assert phone.get("/").json() == {"home": True}

        auth.revoke_all(db)  # "התנתק מכל המכשירים"
        assert phone.get("/").status_code == 303

    with TestClient(app, base_url=f"http://127.0.0.1:{PORT}", client=("127.0.0.1", 50000),
                    follow_redirects=False) as pc:
        assert pc.get("/").json() == {"home": True}  # loopback: no PIN

    with TestClient(app, base_url=f"http://evil.example:{PORT}", client=("127.0.0.1", 50000),
                    follow_redirects=False) as rebinding:
        assert rebinding.get("/").status_code == 403


def test_end_to_end_lockout_over_http(db, clock, fast_scrypt, monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(auth, "lan_addresses", lambda: [LAN_IP])
    auth.set_pin(db, PIN)
    app = _mini_app(make_ctx(db, remote=True))
    with TestClient(app, base_url=f"http://{LAN_IP}:{PORT}", client=(IP, 50000),
                    headers={"X-Agency-Request": "1"}) as phone:
        for _ in range(4):
            assert phone.post("/api/auth/login", json={"pin": WRONG}).status_code == 401
        locked = phone.post("/api/auth/login", json={"pin": WRONG})
        assert locked.status_code == 429 and locked.headers["retry-after"] == "600"
        assert locked.json()["locked_minutes"] == 10
        assert phone.post("/api/auth/login", json={"pin": PIN}).status_code == 429
        clock.advance(auth.LOCKOUT_S)
        assert phone.post("/api/auth/login", json={"pin": PIN}).status_code == 200
