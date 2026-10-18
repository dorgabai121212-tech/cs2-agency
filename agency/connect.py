"""Connections ("חיבורים"): per-platform state, real read-only verification, keys, disconnect.

- :func:`verify` makes ONE read-only call per platform (15s timeout) and answers
  ``{connected, account_name, problem}`` with a plain-Hebrew ``problem``. Results are cached
  in the DB (kv ``connect_verify``) for 10 minutes. The cache holds results only — never
  secret values (its fingerprint is an HMAC under a key that lives only in this process).
- :func:`state` / :func:`platform_state` build the tiles of the connections page.
- :func:`save_keys` stores keys entered in the wizard (OS keyring ``cs2-agency`` when it
  works, else ``.env``; Meta ids go to config), then verifies.
- :func:`disconnect` removes only OUR stored secrets; the old studio's ``cs2studio`` keyring
  entries are never touched.
- YouTube direct OAuth (secondary path; uploads go through Zernio by default):
  :func:`save_client_secret`, :func:`start_youtube_oauth`, :func:`youtube_oauth_state`.

Nothing here returns, logs or raises with a secret value in it.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .config import (SHARED_CONNECTION, Settings, delete_secret, secret, secret_source,
                     store_secret)
from .platforms import registry

log = logging.getLogger("agency.connect")

VERIFY_TTL_S = 600
UNREACHABLE_TTL_S = 60  # "could not check" (offline / service down) is re-checked soon
TIMEOUT_S = 15
CACHE_KEY = "connect_verify"
GRAPH = "https://graph.facebook.com/v24.0"
ZERNIO_API = "https://zernio.com/api"
TELEGRAM_API = "https://api.telegram.org"
X_ME_URL = "https://api.x.com/2/users/me"
TIKTOK_ME_URL = "https://open.tiktokapis.com/v2/user/info/?fields=display_name,username"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YOUTUBE_SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
                  "https://www.googleapis.com/auth/youtube.readonly",
                  "https://www.googleapis.com/auth/youtube.force-ssl"]
CLIENT_SECRET_FILE = "youtube_client_secret.json"
TOKEN_FILE = "youtube_token.json"

CONNECT_KEYS: list[str] = [*registry.ALL_KEYS, "telegram"]
NAMES_HE = {"youtube": "יוטיוב", "youtube_shorts": "יוטיוב Shorts", "tiktok": "טיקטוק",
            "instagram_reels": "אינסטגרם", "facebook": "פייסבוק", "x": "X",
            "reddit": "רדיט", "discord": "דיסקורד", "telegram": "טלגרם"}
ZERNIO_ACCOUNT = {"youtube": "youtube", "youtube_shorts": "youtube", "tiktok": "tiktok"}
ID_CONFIG = {"IG_USER_ID": "meta.ig_user_id", "FB_PAGE_ID": "meta.page_id"}

BAD_KEY_HE = "המפתח לא נכון או שפג תוקפו"
NO_NET_HE = "השירות לא עונה כרגע (אין אינטרנט?) — נסה שוב עוד רגע"
DOWN_HE = "השירות לא עונה כרגע — אבדוק שוב עוד מעט"
RATE_HE = "יותר מדי בדיקות — נסה שוב בעוד כמה דקות"
PARTIAL_HE = "חסרים עוד פרטים בחיבור"
UNKNOWN_HE = "הבדיקה נכשלה — נסה שוב"
# Problems that say nothing about the saved keys (no internet / the service is down): the
# tile shows "not checked right now" instead of "needs fixing", and the result is re-checked
# after UNREACHABLE_TTL_S.
UNREACHABLE = frozenset({NO_NET_HE, DOWN_HE})

_FP_KEY = os.urandom(32)  # never persisted: cached fingerprints are useless after restart
_cache_lock = threading.Lock()


class ConnectError(ValueError):
    """A connect action was refused. Message is owner-facing Hebrew (names only)."""


# ----------------------------------------------------------------------------- helpers
def _result(connected: bool, account_name: str | None = None, problem: str | None = None,
            **extra: Any) -> dict:
    out = {"connected": bool(connected), "account_name": account_name or None,
           "problem": problem or None}
    out.update({k: v for k, v in extra.items() if v is not None})
    if not connected and problem in UNREACHABLE:
        out["unreachable"] = True
    return out


def _check_key(key: str) -> str:
    if key not in CONNECT_KEYS:
        raise ConnectError(f"פלטפורמה לא מוכרת: {key}")
    return key


def _db(settings: Settings, db=None):
    if db is not None:
        return db
    from .db import DB
    return DB(settings.db_path)


def route(key: str, settings: Settings) -> str | None:
    """``"zernio"`` / ``"direct"`` for platforms, None for telegram."""
    return None if key == "telegram" else registry.route(key, settings)


def _adapter(key: str, settings: Settings):
    return registry.get_adapter(key, settings)


def required_names(key: str, settings: Settings) -> list[str]:
    """Secret NAMES this platform needs on its current route."""
    if key == "telegram":
        return ["TELEGRAM_BOT_TOKEN"]
    try:
        return list(_adapter(key, settings).required_secrets)
    except Exception:
        return []


def _value(key: str, name: str, settings: Settings) -> str | None:
    """Credential value (adapter-aware: config ids / secret files count)."""
    if key == "telegram":
        return secret(name)
    try:
        return _adapter(key, settings).credential(name)
    except Exception:
        return secret(name)


def _source_of(key: str, name: str, settings: Settings) -> str | None:
    src = secret_source(name)
    if src:
        return src
    return "config" if _value(key, name, settings) else None


def _configured(key: str, settings: Settings) -> bool:
    if key == "telegram":
        return bool(secret("TELEGRAM_BOT_TOKEN"))
    try:
        return bool(_adapter(key, settings).configured())
    except Exception:
        return False


def platforms_using(names: Iterable[str], settings: Settings) -> list[str]:
    """Connect keys whose current route needs any of ``names``."""
    names = set(names)
    return [k for k in CONNECT_KEYS if names & set(required_names(k, settings))]


def _status_problem(code: int) -> str:
    if code in (401, 403):
        return BAD_KEY_HE
    if code == 429:
        return RATE_HE
    if code == 404:
        return "החשבון לא נמצא — בדוק את המזהה"
    if code >= 500:
        return DOWN_HE
    return UNKNOWN_HE


def _http(method: str, url: str, **kwargs) -> Any:
    import requests

    kwargs.setdefault("timeout", TIMEOUT_S)
    return requests.request(method, url, **kwargs)


def _json(resp) -> dict:
    try:
        data = resp.json() if resp.content else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {"data": data}


def _guard(fn: Callable[[], dict]) -> dict:
    """Run a checker; network trouble / bugs become a plain problem (no exception text)."""
    try:
        return fn()
    except ImportError:
        return _result(False, problem="חסר רכיב בהתקנה — הרץ שוב את ההתקנה")
    except Exception as exc:
        name = type(exc).__name__
        log.info("verification failed: %s", name)
        if any(t in name for t in ("Timeout", "Connection", "SSLError", "ProxyError")):
            return _result(False, problem=NO_NET_HE)
        return _result(False, problem=UNKNOWN_HE)


# ----------------------------------------------------------------------------- checkers
def _zernio_accounts(memo: dict | None = None) -> tuple[list[dict] | None, str | None]:
    """GET /v1/accounts once per :func:`state` call (``memo`` is shared by its threads)."""
    if memo is None:
        return _fetch_zernio_accounts()
    with memo.setdefault("_lock", threading.Lock()):
        if "zernio" not in memo:
            memo["zernio"] = _fetch_zernio_accounts()
        return memo["zernio"]


def _fetch_zernio_accounts() -> tuple[list[dict] | None, str | None]:
    key = secret("ZERNIO_API_KEY")
    resp = _http("GET", f"{ZERNIO_API}/v1/accounts", headers={"Authorization": f"Bearer {key}"})
    if resp.status_code >= 400:
        return None, _status_problem(resp.status_code)
    accounts = _json(resp).get("accounts") or []
    return [a for a in accounts if isinstance(a, dict)], None


def _check_zernio(key: str, settings: Settings, memo: dict | None = None) -> dict:
    accounts, problem = _zernio_accounts(memo)
    if accounts is None:
        return _result(False, problem=problem)
    want = ZERNIO_ACCOUNT[key]
    mine = [a for a in accounts if str(a.get("platform") or "").lower() == want]
    if not mine:
        return _result(False, problem=f"{NAMES_HE[want]} לא מחובר בחשבון ה-Zernio — "
                                      "חבר אותו באתר Zernio")
    good = [a for a in mine if a.get("isActive", True) and not a.get("needsReconnection")]
    acc = (good or mine)[0]
    name = acc.get("displayName") or acc.get("username")
    if not good:
        return _result(False, name, f"צריך לחבר מחדש את {NAMES_HE[want]} באתר Zernio")
    return _result(True, name)


def _meta_check(node_id: str | None, fields: str, label: str) -> dict:
    token = secret("META_ACCESS_TOKEN")
    if not node_id:
        return _result(False, problem=f"חסר המזהה של {label}")
    resp = _http("GET", f"{GRAPH}/{node_id}", params={"fields": fields},
                 headers={"Authorization": f"OAuth {token}"})
    data = _json(resp)
    if resp.status_code >= 400 or "error" in data:
        err = data.get("error") if isinstance(data.get("error"), dict) else {}
        if err.get("code") in (190, 102) or resp.status_code in (401, 403):
            return _result(False, problem=BAD_KEY_HE)
        if err.get("code") in (100, 803) or resp.status_code == 404:
            return _result(False, problem=f"לא מצאתי את {label} — בדוק את המזהה")
        return _result(False, problem=_status_problem(resp.status_code))
    return _result(True, data.get(fields.split(",")[0]))


def _check_instagram(settings: Settings) -> dict:
    return _meta_check(_value("instagram_reels", "IG_USER_ID", settings), "username",
                       "חשבון האינסטגרם")


def _check_facebook(settings: Settings) -> dict:
    return _meta_check(_value("facebook", "FB_PAGE_ID", settings), "name", "עמוד הפייסבוק")


def _check_telegram(settings: Settings) -> dict:
    token = secret("TELEGRAM_BOT_TOKEN") or ""
    # The token is part of the URL path: never log the URL or the exception text.
    resp = _http("GET", f"{TELEGRAM_API}/bot{token}/getMe")
    data = _json(resp)
    if resp.status_code in (401, 404):
        return _result(False, problem=BAD_KEY_HE)
    if resp.status_code >= 400 or not data.get("ok"):
        return _result(False, problem=_status_problem(resp.status_code))
    user = data.get("result") or {}
    name = user.get("username")
    return _result(True, f"@{name}" if name else user.get("first_name"))


def _check_x(settings: Settings) -> dict:
    from requests_oauthlib import OAuth1

    auth = OAuth1(secret("X_API_KEY"), secret("X_API_SECRET"), secret("X_ACCESS_TOKEN"),
                  secret("X_ACCESS_SECRET"))
    resp = _http("GET", X_ME_URL, auth=auth)
    if resp.status_code >= 400:
        if resp.status_code == 403:
            return _result(False, problem="לאפליקציה ב-X אין הרשאת קריאה/כתיבה — בדוק בהגדרות")
        return _result(False, problem=_status_problem(resp.status_code))
    user = _json(resp).get("data") or {}
    return _result(True, f"@{user['username']}" if user.get("username") else user.get("name"))


def _check_reddit(settings: Settings) -> dict:
    import praw

    username = secret("REDDIT_USERNAME") or ""
    reddit = praw.Reddit(client_id=secret("REDDIT_CLIENT_ID"),
                         client_secret=secret("REDDIT_CLIENT_SECRET"),
                         username=username, password=secret("REDDIT_PASSWORD"),
                         user_agent=f"cs2-agency/0.2 (by u/{username})",
                         timeout=TIMEOUT_S, check_for_async=False)
    try:
        me = reddit.user.me()
    except Exception as exc:
        text = type(exc).__name__
        if "OAuth" in text or "Forbidden" in text or "ResponseException" in text:
            return _result(False, problem="שם המשתמש, הסיסמה או המפתחות לא נכונים")
        raise
    return _result(bool(me), f"u/{me.name}" if me else None,
                   None if me else "שם המשתמש, הסיסמה או המפתחות לא נכונים")


def _check_discord(settings: Settings) -> dict:
    url = secret("DISCORD_WEBHOOK_URL") or ""
    if not url.startswith(("https://discord.com/api/webhooks/",
                           "https://discordapp.com/api/webhooks/",
                           "https://canary.discord.com/api/webhooks/",
                           "https://ptb.discord.com/api/webhooks/")):
        return _result(False, problem="זה לא נראה כמו הקישור שמעתיקים מדיסקורד")
    resp = _http("GET", url)
    if resp.status_code in (401, 404):
        return _result(False, problem="הקישור לא נכון או שהוא נמחק בדיסקורד")
    if resp.status_code >= 400:
        return _result(False, problem=_status_problem(resp.status_code))
    return _result(True, _json(resp).get("name"))


def _check_tiktok_direct(settings: Settings) -> dict:
    resp = _http("GET", TIKTOK_ME_URL,
                 headers={"Authorization": f"Bearer {secret('TIKTOK_ACCESS_TOKEN')}"})
    data = _json(resp)
    err = data.get("error") if isinstance(data.get("error"), dict) else {}
    if resp.status_code >= 400 or (err.get("code") not in (None, "", "ok")):
        if resp.status_code in (401, 403) or "token" in str(err.get("code", "")):
            return _result(False, problem=BAD_KEY_HE)
        return _result(False, problem=_status_problem(resp.status_code))
    user = (data.get("data") or {}).get("user") or {}
    return _result(True, user.get("display_name") or user.get("username"))


# ----------------------------------------------------------------------------- youtube direct
def _secrets_dir(settings: Settings) -> Path:
    from .settings_store import secrets_dir
    return secrets_dir(settings)


def youtube_token_paths(settings: Settings) -> list[Path]:
    """Where a YouTube authorized-user token may be: ``data/secrets`` (v2), ``data`` (v1)."""
    return [settings.data_dir / "secrets" / TOKEN_FILE, settings.data_dir / TOKEN_FILE]


def _youtube_token(settings: Settings) -> tuple[dict | None, str | None]:
    """(authorized-user JSON, source) from our token file or the studio's ``youtube_oauth``."""
    for path in youtube_token_paths(settings):
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                return data, "agency"
    raw = secret("YOUTUBE_OAUTH_JSON")
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data, secret_source("YOUTUBE_OAUTH_JSON")
    return None, None


def _client_ids(settings: Settings, token: dict) -> tuple[str | None, str | None]:
    if token.get("client_id") and token.get("client_secret"):
        return token["client_id"], token["client_secret"]
    for path in (settings.data_dir / "secrets" / CLIENT_SECRET_FILE,
                 Path(secret("YOUTUBE_CLIENT_SECRETS_FILE") or "")):
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                inner = data.get("installed") or data.get("web") or {}
                return inner.get("client_id"), inner.get("client_secret")
        except (OSError, ValueError, AttributeError):
            continue
    return token.get("client_id"), None


def _access_token(settings: Settings, token: dict) -> tuple[str | None, str | None]:
    """(access token, problem). Refreshes with the refresh token when needed (not saved)."""
    access = token.get("token") or token.get("access_token")
    expiry = token.get("expiry")
    if access and expiry:
        try:
            when = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            if when.timestamp() - time.time() > 60:
                return access, None
        except ValueError:
            pass
    refresh = token.get("refresh_token")
    client_id, client_secret = _client_ids(settings, token)
    if not refresh or not client_id or not client_secret:
        return None, "צריך להתחבר מחדש עם גוגל"
    resp = _http("POST", GOOGLE_TOKEN_URL, data={
        "client_id": client_id, "client_secret": client_secret, "refresh_token": refresh,
        "grant_type": "refresh_token"})
    if resp.status_code >= 400:
        return None, "החיבור לגוגל פג — צריך להתחבר מחדש עם גוגל"
    return _json(resp).get("access_token"), None


def verify_youtube_direct(settings: Settings) -> dict:
    """``channels.list(mine=true)`` with our (or the studio's) Google token. Read-only."""
    def run() -> dict:
        token, source = _youtube_token(settings)
        if token is None:
            return _result(False, problem="עוד לא התחברת עם גוגל")
        access, problem = _access_token(settings, token)
        if not access:
            return _result(False, problem=problem)
        resp = _http("GET", YOUTUBE_CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                     headers={"Authorization": f"Bearer {access}"})
        if resp.status_code >= 400:
            return _result(False, problem=_status_problem(resp.status_code))
        items = _json(resp).get("items") or []
        if not items:
            return _result(False, problem="לחשבון הגוגל הזה אין ערוץ יוטיוב")
        snippet = items[0].get("snippet") or {}
        thumb = ((snippet.get("thumbnails") or {}).get("default") or {}).get("url")
        return _result(True, snippet.get("title"), thumbnail=thumb, token_source=source)
    return _guard(run)


# ----------------------------------------------------------------------------- verify + cache
def _fingerprint(key: str, settings: Settings) -> str:
    """Changes when the route, a value or its source changes. HMAC under a per-process key."""
    parts: list[Any] = [key, route(key, settings)]
    for name in required_names(key, settings):
        value = _value(key, name, settings) or ""
        parts.append([name, _source_of(key, name, settings),
                      hmac.new(_FP_KEY, value.encode("utf-8"), hashlib.sha256).hexdigest()])
    if key in ("youtube", "youtube_shorts") and route(key, settings) == "direct":
        for path in youtube_token_paths(settings):
            parts.append([path.name, path.stat().st_mtime_ns if path.is_file() else 0])
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def _checker(key: str, settings: Settings, memo: dict | None) -> Callable[[], dict]:
    r = route(key, settings)
    if r == "zernio":
        return lambda: _check_zernio(key, settings, memo)
    if key in ("youtube", "youtube_shorts"):
        return lambda: verify_youtube_direct(settings)
    table: dict[str, Callable[[Settings], dict]] = {
        "tiktok": _check_tiktok_direct, "instagram_reels": _check_instagram,
        "facebook": _check_facebook, "x": _check_x, "reddit": _check_reddit,
        "discord": _check_discord, "telegram": _check_telegram}
    fn = table[key]
    return lambda: fn(settings)


def _missing(key: str, settings: Settings) -> list[str]:
    if key == "telegram":
        return [n for n in required_names(key, settings) if not secret(n)]
    try:
        return list(_adapter(key, settings).missing_secrets())
    except Exception:
        return required_names(key, settings)


def _verify_uncached(key: str, settings: Settings, memo: dict | None = None) -> dict:
    names = required_names(key, settings)
    missing = _missing(key, settings)
    if key in ("youtube", "youtube_shorts") and route(key, settings) == "direct":
        token, _src = _youtube_token(settings)
        if token is None:
            has_client = (settings.data_dir / "secrets" / CLIENT_SECRET_FILE).is_file() \
                or Path(secret("YOUTUBE_CLIENT_SECRETS_FILE") or "").is_file()
            return _result(False, problem="עוד לא התחברת עם גוגל" if has_client else None)
        return verify_youtube_direct(settings)
    if missing:
        return _result(False, problem=PARTIAL_HE if len(missing) < len(names) else None)
    if key != "telegram":
        try:
            if not _adapter(key, settings).configured():
                return _result(False, problem="רכיב ההעלאה חסר בהתקנה — עדכן את הסוכנות")
        except Exception:
            return _result(False, problem=UNKNOWN_HE)
    return _guard(_checker(key, settings, memo))


def _cache_get(db, key: str, fp: str, *, any_age: bool = False) -> dict | None:
    """The cached result for ``key`` while its fingerprint matches and it is fresh (or of
    ``any_age``: the quick read of the page shows the last known state)."""
    cache = db.kv_get(CACHE_KEY, {}) or {}
    hit = cache.get(key) if isinstance(cache, dict) else None
    if not isinstance(hit, dict) or hit.get("fp") != fp:
        return None
    result = hit.get("result") if isinstance(hit.get("result"), dict) else {}
    ttl = UNREACHABLE_TTL_S if result.get("unreachable") else VERIFY_TTL_S
    if any_age or time.time() - float(hit.get("ts", 0)) < ttl:
        return dict(result)
    return None


def _from_cache(hit: dict) -> dict:
    """A cached verification result as a :func:`_result` dict."""
    return _result(hit.get("connected", False), hit.get("account_name"), hit.get("problem"),
                   thumbnail=hit.get("thumbnail"))


def _cache_put(db, entries: Mapping[str, tuple[str, dict]]) -> None:
    with _cache_lock:
        cache = db.kv_get(CACHE_KEY, {}) or {}
        if not isinstance(cache, dict):
            cache = {}
        now = time.time()
        for key, (fp, result) in entries.items():
            cache[key] = {"ts": now, "fp": fp, "result": {
                k: result.get(k) for k in ("connected", "account_name", "problem", "thumbnail",
                                           "unreachable")}}
        db.kv_set(CACHE_KEY, cache)


def invalidate(settings: Settings, key: str | None = None, db=None) -> None:
    """Forget cached verification for ``key`` (and what shares its connection) or all.

    For a Zernio platform the upload adapter's account cache (kv ``zernio.accounts``) is
    dropped too, so a key saved / removed / checked again here is what the next upload uses
    (a reconnected account is not refused for hours from a stale cache).
    """
    db = _db(settings, db)
    if key is None or key in ZERNIO_ACCOUNT:
        try:
            from .platforms.zernio import ACCOUNTS_KV
            db.kv_set(ACCOUNTS_KV, None)
        except Exception:  # pragma: no cover - the cache is only an optimisation
            log.debug("could not clear the Zernio account cache", exc_info=True)
    with _cache_lock:
        cache = db.kv_get(CACHE_KEY, {}) or {}
        if key is None or not isinstance(cache, dict):
            cache = {}
        else:
            group = {key, *[k for k, v in SHARED_CONNECTION.items() if v == key],
                     SHARED_CONNECTION.get(key, key)}
            for k in group:
                cache.pop(k, None)
        db.kv_set(CACHE_KEY, cache)


def verify(platform: str, settings: Settings, *, use_cache: bool = True, db=None) -> dict:
    """Read-only check that ``platform`` really works: ``{connected, account_name, problem}``.

    Zernio route: ``GET /v1/accounts`` and find the platform's account. Instagram/Facebook:
    Graph v24 ``GET /<id>?fields=username|name``. Telegram ``getMe``. X ``/2/users/me``.
    Reddit ``user.me()``. Discord ``GET <webhook>``. Direct YouTube ``channels.list mine``.
    Not configured → no network at all. Cached for 10 minutes.
    """
    key = _check_key(platform)
    db = _db(settings, db)
    fp = _fingerprint(key, settings)
    if use_cache:
        hit = _cache_get(db, key, fp)
        if hit is not None:
            return _from_cache(hit)
    result = _verify_uncached(key, settings)
    _cache_put(db, {key: (fp, result)})
    return result


# ----------------------------------------------------------------------------- state
def _source(key: str, settings: Settings) -> str | None:
    """'studio' when the connection rides on the old studio's keyring, 'agency' when it is
    ours, None when nothing is set."""
    if key in ("youtube", "youtube_shorts") and route(key, settings) == "direct":
        _token, src = _youtube_token(settings)
        if src:
            return "studio" if src == "studio" else "agency"
    sources = [_source_of(key, n, settings) for n in required_names(key, settings)]
    if "studio" in sources:
        return "studio"
    return "agency" if any(sources) else None


def platform_state(key: str, settings: Settings, db=None, *, check: bool = True,
                   _verified: dict | None = None) -> dict:
    """One connections tile::

        {key, name_he, connected, account_name, live, live_switch, via, source, shares,
         required:[names], missing:[names], problem, unreachable, checked}

    ``check=False`` makes no network call (presence + the last known check, of any age).
    Never includes secret values.
    """
    key = _check_key(key)
    db = _db(settings, db)
    missing = _missing(key, settings)
    configured = False
    if key == "telegram":
        configured = not missing
    else:
        try:
            configured = bool(_adapter(key, settings).configured())
        except Exception:
            configured = False
    checked = True
    if _verified is not None:
        result = _verified
    elif check and (configured or route(key, settings) == "direct"
                    and key in ("youtube", "youtube_shorts")):
        result = verify(key, settings, db=db)
    else:
        cached = _cache_get(db, key, _fingerprint(key, settings), any_age=True)
        checked = cached is not None
        cached = cached or {}
        result = _result(configured and cached.get("connected", True),
                         cached.get("account_name"), cached.get("problem"))
        if not configured:
            names = required_names(key, settings)
            result = _result(False, problem=PARTIAL_HE if 0 < len(missing) < len(names)
                             else None)
    live_switch = False if key == "telegram" else settings.platform_live(key)
    # Same rule as the home screen (home.live_platforms): planned (enabled), switched live and
    # connected. A live switch on a platform the staff never plans for is not "live".
    planned = key != "telegram" and key in registry.enabled_keys(settings)
    connected = bool(result.get("connected"))
    out = {
        "key": key,
        "name_he": NAMES_HE.get(key, key),
        "connected": connected,
        "account_name": result.get("account_name"),
        "live": bool(live_switch and configured and planned),
        "live_switch": bool(live_switch),
        "via": route(key, settings),
        "source": _source(key, settings),
        "shares": SHARED_CONNECTION.get(key),
        "required": required_names(key, settings),
        "missing": missing,
        "problem": result.get("problem"),
        # True: the check could not reach the service (offline / service down) — this says
        # nothing about the saved keys. ``checked`` False: presence only, never verified.
        "unreachable": bool(result.get("unreachable")),
        "checked": bool(checked and configured),
    }
    if result.get("thumbnail"):
        out["thumbnail"] = result["thumbnail"]
    return out


def state(settings: Settings, db=None, *, check: bool = True,
          keys: Iterable[str] | None = None) -> list[dict]:
    """All tiles (platforms in canonical order; pass ``keys`` to include ``"telegram"``).

    With ``check`` the uncached verifications run in parallel (≤ ~15s total).
    """
    db = _db(settings, db)
    keys = list(keys) if keys is not None else list(registry.ALL_KEYS)
    for k in keys:
        _check_key(k)
    verified: dict[str, dict] = {}
    if check:
        memo: dict = {}
        todo: list[tuple[str, str]] = []
        for k in keys:
            fp = _fingerprint(k, settings)
            hit = _cache_get(db, k, fp)
            if hit is not None:
                verified[k] = _from_cache(hit)
            else:
                todo.append((k, fp))
        if todo:
            with ThreadPoolExecutor(max_workers=min(8, len(todo))) as pool:
                futures = {k: (fp, pool.submit(_verify_uncached, k, settings, memo))
                           for k, fp in todo}
            fresh = {k: (fp, f.result()) for k, (fp, f) in futures.items()}
            _cache_put(db, fresh)
            verified.update({k: r for k, (_fp, r) in fresh.items()})
    return [platform_state(k, settings, db, check=check, _verified=verified.get(k))
            for k in keys]


# ----------------------------------------------------------------------------- keys
def save_keys(platform: str, values: Mapping[str, Any], settings: Settings, db=None) -> dict:
    """Store keys typed in the wizard, then verify for real.

    Only names this platform needs are accepted. Empty values are ignored (keep the old
    one). Secrets → OS keyring ``cs2-agency`` (else ``.env``); Meta ids → config.
    Returns the tile (:func:`platform_state`) plus ``saved: [names]``. Never echoes values.
    """
    from .settings_store import SettingsError, update_config

    key = _check_key(platform)
    allowed = set(required_names(key, settings))
    if not isinstance(values, Mapping):
        raise ConnectError("פרטי החיבור לא תקינים")
    unknown = [n for n in values if n not in allowed]
    if unknown:
        raise ConnectError(f"שדה לא מוכר לחיבור הזה: {', '.join(map(str, unknown))}")
    saved: list[str] = []
    meta_patch: dict[str, str] = {}
    try:
        for name, raw in values.items():
            value = raw.strip() if isinstance(raw, str) else raw
            if value in (None, ""):
                continue
            if not isinstance(value, str):
                raise ConnectError(f"הערך של {name} חייב להיות טקסט")
            if name == "YOUTUBE_CLIENT_SECRETS_FILE":
                raise ConnectError("את קובץ הגוגל מעלים בשלב הקובץ באשף")
            if name in ID_CONFIG:
                meta_patch[ID_CONFIG[name].split(".", 1)[1]] = value
            else:
                store_secret(name, value, settings.root)
            saved.append(name)
        if meta_patch:
            update_config(settings, {"meta": meta_patch})
    except SettingsError as exc:
        raise ConnectError(str(exc)) from None
    db = _db(settings, db)
    for k in platforms_using(saved, settings) or [key]:
        invalidate(settings, k, db)
    verify(key, settings, use_cache=False, db=db)
    return {**platform_state(key, settings, db), "saved": saved}


def disconnect(platform: str, settings: Settings, db=None) -> dict:
    """Remove OUR stored secrets for ``platform`` (after the owner confirmed).

    - a secret shared with another connected platform is kept (``kept_shared``) only when
      that platform would stay connected without the secrets being removed here (Meta's
      token stays for Facebook when Instagram is disconnected). When the other platform
      needs nothing else (YouTube and TikTok share one Zernio key) the key IS removed and
      that platform is disconnected as well (``also_disconnected``, live switch off too)
    - secrets coming from the old studio's keyring are never touched (``studio``); values
      set in the computer's own environment (not our ``.env``) cannot be removed by us and
      are reported (``system``)
    - the platform's live switch is turned off; YouTube direct also drops our token file;
      Telegram also forgets the paired chat
    Returns ``{key, removed:[names/files], kept_shared:[names], studio:[names],
    system:[names], also_disconnected:[keys], state}``.
    """
    from .settings_store import update_config

    key = _check_key(platform)
    base = SHARED_CONNECTION.get(key, key)
    group = {base, *[k for k, v in SHARED_CONNECTION.items() if v == base]}
    db = _db(settings, db)
    removed: list[str] = []
    kept: list[str] = []
    studio: list[str] = []
    system: list[str] = []
    removing = set(required_names(key, settings))
    configured_before = [k for k in CONNECT_KEYS if k not in group and _configured(k, settings)]
    # Keepers: connected platforms that need something besides what we remove here.
    others = [k for k in configured_before
              if not set(required_names(k, settings)) <= removing]
    for name in required_names(key, settings):
        if any(name in required_names(k, settings) for k in others):
            kept.append(name)
            continue
        had = False
        if secret_source(name) in ("env", "keyring"):
            where = delete_secret(name, settings.root)
            if "system_env" in where:
                system.append(name)
            had = any(w in ("env", "keyring") for w in where)
        if name in ID_CONFIG and settings.get(ID_CONFIG[name]):
            update_config(settings, {"meta": {ID_CONFIG[name].split(".", 1)[1]: ""}})
            had = True
        if had:
            removed.append(name)
        if secret_source(name) == "studio":
            studio.append(name)
    if key in ("youtube", "youtube_shorts") and route(key, settings) == "direct":
        for path in youtube_token_paths(settings):
            if path.is_file():
                try:
                    path.unlink()
                    removed.append(path.name)
                except OSError:
                    pass
        if secret_source("YOUTUBE_OAUTH_JSON") == "studio":
            studio.append("YOUTUBE_OAUTH_JSON")
    also = [k for k in configured_before if k != key and not _configured(k, settings)]
    if key == "telegram":
        try:
            from types import SimpleNamespace

            from .notify.telegram import unpair
            unpair(SimpleNamespace(settings=settings, db=db))
        except Exception as exc:  # pragma: no cover - the bot token is gone anyway
            log.info("could not forget the Telegram chat: %s", type(exc).__name__)
    patch = {k: {"live": False} for k in [*group, *also] if k in registry.ALL_KEYS}
    if patch:
        update_config(settings, {"publishing": {"platforms": patch}})
    for k in [*group, *also]:
        invalidate(settings, k, db)
    return {"key": key, "removed": removed, "kept_shared": kept, "studio": studio,
            "system": system, "also_disconnected": also,
            "state": platform_state(key, settings, db, check=False)}


# ----------------------------------------------------------------------------- youtube oauth
_oauth_lock = threading.Lock()
_oauth_state: dict[str, Any] = {"state": "idle", "channel": None, "thumbnail": None,
                                "problem": None}


def save_client_secret(settings: Settings, data: bytes | str | Mapping) -> dict:
    """Validate a Google ``client_secret*.json`` ("installed"/"web") and store it as
    ``data/secrets/youtube_client_secret.json`` (owner-only). Returns ``{"ok": True,
    "kind": "installed"|"web"}``; raises :class:`ConnectError` (Hebrew) when invalid."""
    from .importer import is_client_secret
    from .settings_store import write_private_file

    if isinstance(data, (bytes, bytearray)):
        if len(data) > 256 * 1024:
            raise ConnectError("הקובץ גדול מדי — זה לא קובץ ההתחברות של גוגל")
        data = data.decode("utf-8-sig", "replace")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            raise ConnectError("זה לא קובץ ההתחברות של גוגל (client_secret.json)") from None
    if not is_client_secret(data):
        raise ConnectError("זה לא קובץ ההתחברות של גוגל (client_secret.json)")
    kind = "installed" if "installed" in data else "web"
    write_private_file(_secrets_dir(settings) / CLIENT_SECRET_FILE,
                       json.dumps(dict(data), ensure_ascii=False, indent=2))
    invalidate(settings, "youtube")
    return {"ok": True, "kind": kind}


def youtube_oauth_state() -> dict:
    """``{state: idle|waiting|done|error, channel, thumbnail, problem}`` for polling."""
    with _oauth_lock:
        return dict(_oauth_state)


def _set_oauth(**values: Any) -> None:
    with _oauth_lock:
        _oauth_state.update(values)


def _run_oauth(settings: Settings, open_browser: bool) -> None:
    from .settings_store import write_private_file

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow

        client = _secrets_dir(settings) / CLIENT_SECRET_FILE
        if not client.is_file():
            env_file = Path(secret("YOUTUBE_CLIENT_SECRETS_FILE") or "")
            client = env_file if env_file.is_file() else client
        if not client.is_file():
            _set_oauth(state="error", problem="חסר קובץ ההתחברות של גוגל — העלה אותו קודם")
            return
        flow = InstalledAppFlow.from_client_secrets_file(str(client), YOUTUBE_SCOPES)
        creds = flow.run_local_server(port=0, open_browser=open_browser, timeout_seconds=300)
        if creds is None:
            _set_oauth(state="error", problem="ההתחברות לא הושלמה — נסה שוב")
            return
        write_private_file(_secrets_dir(settings) / TOKEN_FILE, creds.to_json())
        invalidate(settings, "youtube")
        result = verify_youtube_direct(settings)
        if result.get("connected"):
            _set_oauth(state="done", channel=result.get("account_name"),
                       thumbnail=result.get("thumbnail"), problem=None)
        else:
            _set_oauth(state="error", problem=result.get("problem") or UNKNOWN_HE)
    except ImportError:
        _set_oauth(state="error", problem="חסר רכיב בהתקנה — הרץ שוב את ההתקנה")
    except Exception as exc:  # never surface exception text (could contain codes/urls)
        log.info("youtube oauth failed: %s", type(exc).__name__)
        _set_oauth(state="error", problem="ההתחברות לגוגל נכשלה — נסה שוב")


def start_youtube_oauth(settings: Settings, *, open_browser: bool = True,
                        background: bool = True) -> dict:
    """Start Google's installed-app login (browser on this PC) in a background thread.

    Secondary path: uploads go through Zernio by default; this token is for direct mode and
    stats/comments. Returns the current :func:`youtube_oauth_state`.
    """
    with _oauth_lock:
        if _oauth_state.get("state") == "waiting":
            return dict(_oauth_state)
        _oauth_state.update(state="waiting", channel=None, thumbnail=None, problem=None)
    if background:
        threading.Thread(target=_run_oauth, args=(settings, open_browser), daemon=True,
                         name="youtube-oauth").start()
    else:
        _run_oauth(settings, open_browser)
    return youtube_oauth_state()


__all__ = ["CONNECT_KEYS", "ConnectError", "NAMES_HE", "disconnect", "invalidate",
           "platform_state", "platforms_using", "required_names", "save_client_secret",
           "save_keys", "start_youtube_oauth", "state", "verify", "verify_youtube_direct",
           "youtube_oauth_state"]
