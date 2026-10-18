"""Telegram approvals (SPEC_V2 §11.5): one-tap "אשר הכל" from the phone, like the old studio.

- Optional: ``notify.telegram.enabled: true`` + the bot token ``TELEGRAM_BOT_TOKEN`` (env →
  keyring → the old studio's ``telegram_bot`` entry, read-only).
- Pairing: the dashboard shows a 6-digit code (:func:`new_pairing_code`); the owner sends
  ``/start <code>`` to the bot in a private chat. The chat id is saved in config
  ``notify.telegram.chat_id`` (and in the DB kv as a fallback). Only that private chat is ever
  obeyed; everyone else is ignored silently. A new code (re-pairing) replaces the paired chat;
  the dashboard unpairs first so a lost / compromised phone loses its buttons at once. Wrong
  codes are counted per chat (a stranger cannot cancel the owner's code by guessing).
- Cards: :meth:`TelegramNotifier.send_approval_card` sends (for publish / spend approvals)
  the full texts that will be published, then one Hebrew card with two buttons,
  "✅ אשר הכל" and "⏸ לא עכשיו". Every button's callback data is HMAC-SHA256 signed (key
  ``AGENCY_HMAC_KEY``, generated once, kept in the OS keyring ``cs2-agency`` or a private file
  in ``data/secrets``) and bound to the paired chat id, to this database (a random id kept in
  kv) and to the approval's creation time, so an old card never works for a different
  approval that later got the same number. "אשר הכל" runs exactly what the dashboard runs:
  ``db.decide_approval(id, True)`` then ``pipeline.execute_approval`` — but only while each
  platform's training / real-upload state is still what the card showed; otherwise a fresh
  card is sent and nothing is published.
  "לא עכשיו" leaves the approval waiting in the dashboard (nothing is rejected).
- :meth:`TelegramNotifier.send_published` sends "עלה!" with the real link (never for
  training-mode posts).
- Long polling (``getUpdates`` with timeout + offset) in a background thread. Two pollers on one
  bot conflict: HTTP 409 means the old studio daemon is still polling — state
  ``busy_by_old_studio`` ("הבוט תפוס ע״י הסטודיו הישן"), nothing is sent, the dashboard stays
  the only channel, and polling retries slowly until the old one stops.
- The token is part of every Bot API URL, so URLs and exception texts are never logged or put
  in errors; only method names and Telegram's own descriptions are.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import re
import secrets as pysecrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..config import Settings, secret
from ..events import emit
from ..models import Approval

log = logging.getLogger("agency.notify.telegram")

_BOT_PATH_RE = re.compile(r"/bot\d+:[\w-]+")


class _RedactBotToken(logging.Filter):
    """urllib3 logs request lines ("POST /bot<token>/getUpdates") at DEBUG: redact them."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record: leave it alone
            return True
        if "/bot" in message and _BOT_PATH_RE.search(message):
            record.msg, record.args = _BOT_PATH_RE.sub("/bot***", message), None
        return True


def _install_log_redaction() -> None:
    for name in ("urllib3.connectionpool", "requests"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _RedactBotToken) for f in logger.filters):
            logger.addFilter(_RedactBotToken())


_install_log_redaction()

API_BASE = "https://api.telegram.org"
HMAC_KEY_NAME = "AGENCY_HMAC_KEY"
HMAC_FILE = "agency_hmac_key"
SIG_VERSION = "v1"
SIG_HEX = 24                    # 96-bit MAC; callback_data stays far below Telegram's 64 bytes
ACTIONS = {"A": "approve", "L": "later"}
# Only kinds whose card shows everything that matters get buttons (publish: platforms, times and
# the full texts; spend: the amount and the reason). Replies are approved in the dashboard.
CARD_KINDS = ("publish", "spend")

CHAT_KV = "telegram.chat"               # {"chat_id": "123" | None, "name", "paired_at"}
OFFSET_KV = "telegram.offset"
STATE_KV = "telegram.state"             # {"state", "message_he", "ts"} for the dashboard
PAIRING_KV = "telegram.pairing"         # {"hash", "expires", "fails": {chat: n}, "total"}
CARDS_KV = "telegram.cards"             # {approval id: {message_id, chat_id, snoozed, closed,
                                        #  live: {platform: bool}}}
DB_ID_KV = "telegram.db_id"             # random id of this database (binds the buttons)
SENT_KV = "telegram.published_sent"     # [post ids already announced]; baseline at pairing

PAIRING_TTL_S = 15 * 60
MAX_PAIR_FAILS = 5                      # wrong codes per chat; then that chat is ignored
MAX_PAIR_FAILS_TOTAL = 50               # all chats together; then the code is cancelled
LONG_POLL_S = 25
CONFLICT_BACKOFF_S = 60.0
ERROR_BACKOFF_S = 30.0
MAX_CARDS_PER_SYNC = 3
PREVIEW_MAX_BYTES = 49 * 1024 * 1024    # bots may upload ≤50 MB
TEXT_LIMIT = 3900

STATES_HE = {
    "off": "האישורים בטלגרם כבויים",
    "not_configured": "צריך להדביק את המפתח של הבוט",
    "not_paired": "הבוט מוכן — שלח לו את קוד הצימוד מלוח הבקרה",
    "running": "הבוט פעיל — אישורים יגיעו לטלגרם",
    "busy_by_old_studio": "הבוט תפוס ע״י הסטודיו הישן — בינתיים מאשרים רק בלוח הבקרה",
    "bad_token": "המפתח של הבוט לא נכון או שפג תוקפו",
    "error": "אין חיבור לטלגרם כרגע — ננסה שוב עוד מעט",
    "stopped": "הבוט לא פועל כרגע",
}

PLATFORM_HE = {"youtube": "יוטיוב", "youtube_shorts": "יוטיוב שורטס", "tiktok": "טיקטוק",
               "instagram_reels": "אינסטגרם", "facebook": "פייסבוק", "x": "X",
               "reddit": "רדיט", "discord": "דיסקורד"}
PLATFORM_ICON = {"youtube": "📺", "youtube_shorts": "📱", "tiktok": "🎵",
                 "instagram_reels": "📸", "facebook": "👍", "x": "✖️", "reddit": "👽",
                 "discord": "💬"}
DAYS_HE = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]

BTN_APPROVE = "✅ אשר הכל"
BTN_LATER = "⏸ לא עכשיו"
PAIR_HELP_HE = ("אם זה הבוט שלך — פתח את לוח הבקרה ← חיבורים ← טלגרם, "
                "ושלח לי את הקוד שמופיע שם.")
CHANGED_HE = "המצב השתנה — שלחתי לך כרטיס מעודכן"
HELP_HE = ("אני הבוט של הסוכנות 🎮\n"
           "כשמשהו מחכה לאישור שלך — אשלח לך כרטיס עם כפתור \"אשר הכל\".\n"
           "כשסרטון עולה — אשלח לך קישור.")


class TelegramError(RuntimeError):
    """A Bot API call failed. Never contains the token (no URLs, no exception text)."""

    def __init__(self, method: str, description: str, code: int | None = None,
                 retry_after: float | None = None):
        super().__init__(f"telegram {method}: {description}")
        self.method = method
        self.code = code
        self.retry_after = retry_after


def esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""), quote=False)


# ------------------------------------------------------------------------------- HMAC key
def _keyring():
    """The core's keyring module accessor (honours ``AGENCY_KEYRING=off``), or None."""
    try:
        from ..config import _keyring_module
        return _keyring_module()
    except Exception:
        return None


def hmac_key(settings: Settings) -> bytes:
    """The callback-signing key: ``AGENCY_HMAC_KEY`` (env / keyring ``cs2-agency``), else the
    private file ``data/secrets/agency_hmac_key``; generated once when missing. Never logged."""
    value = secret(HMAC_KEY_NAME)
    path = None
    if not value:
        from ..settings_store import secrets_dir
        path = secrets_dir(settings) / HMAC_FILE
        try:
            value = path.read_text(encoding="utf-8").strip() if path.is_file() else None
        except OSError:
            value = None
    if value:
        return value.encode("utf-8")
    value = pysecrets.token_hex(32)
    stored = False
    kr = _keyring()
    if kr is not None:
        try:
            from ..config import KEYRING_SERVICE, clear_secret_cache
            kr.set_password(KEYRING_SERVICE, HMAC_KEY_NAME, value)
            clear_secret_cache()
            stored = secret(HMAC_KEY_NAME) == value
        except Exception:
            stored = False
    if not stored:
        from ..settings_store import write_private_file
        write_private_file(path, value)
    return value.encode("utf-8")


def _mac(key: bytes, action: str, approval_id: int, chat_id: Any, bind: str = "") -> str:
    msg = f"{SIG_VERSION}|{action}|{int(approval_id)}|{chat_id}|{bind}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:SIG_HEX]


def sign_callback(key: bytes, action: str, approval_id: int, chat_id: Any,
                  bind: str = "") -> str:
    """``v1|<action>|<approval id>|<mac>`` bound to the paired chat id and ``bind`` (this
    database's id + the approval's creation time, see :meth:`TelegramNotifier.bind_of`).
    ≤64 bytes."""
    if action not in ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    return (f"{SIG_VERSION}|{action}|{int(approval_id)}|"
            f"{_mac(key, action, approval_id, chat_id, bind)}")


def parse_callback(data: Any) -> tuple[str, int] | None:
    """(action, approval id) of a well-formed callback — NOT verified (see verify_callback)."""
    if not isinstance(data, str) or len(data) > 64:
        return None
    parts = data.split("|")
    if len(parts) != 4 or parts[0] != SIG_VERSION or parts[1] not in ACTIONS \
            or not parts[2].isdigit():
        return None
    return parts[1], int(parts[2])


def verify_callback(key: bytes, data: Any, chat_id: Any, bind: str = ""
                    ) -> tuple[str, int] | None:
    """(action, approval id) when ``data`` was signed by us for ``chat_id`` + ``bind``."""
    parsed = parse_callback(data)
    if parsed is None:
        return None
    good = _mac(key, parsed[0], parsed[1], chat_id, bind)
    if not hmac.compare_digest(good, str(data).split("|")[3]):
        return None
    return parsed


# ------------------------------------------------------------------------------- formatting
def _tz(settings: Settings):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(settings.get("publishing.timezone", "Asia/Jerusalem") or "Asia/Jerusalem")
    except Exception:
        return timezone(timedelta(hours=3))


def when_he(settings: Settings, iso: str | None, now: datetime | None = None) -> str:
    """"היום 17:00" / "מחר 14:30" / "יום שישי 23.10 20:00" / "עכשיו"."""
    if not iso:
        return "עכשיו"
    try:
        at = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return "עכשיו"
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    tz = _tz(settings)
    at = at.astimezone(tz)
    current = (now or datetime.now(timezone.utc)).astimezone(tz)
    if at <= current:
        return "עכשיו"
    today = current.date()
    if at.date() == today:
        day = "היום"
    elif at.date() == today + timedelta(days=1):
        day = "מחר"
    else:
        day = f"יום {DAYS_HE[at.weekday()]} {at:%d.%m}"
    return f"{day} {at:%H:%M}"


def _view_from_approval(ctx, approval: Approval) -> dict:
    """Minimal version of the dashboard's ``approval_view`` (no FastAPI import)."""
    view = approval.to_dict()
    payload = approval.payload or {}
    plans = []
    for plan in payload.get("plans") or []:
        if isinstance(plan, dict):
            inner = plan["plan"] if isinstance(plan.get("plan"), dict) else plan
            plans.append({"platform": inner.get("platform", ""), "title": inner.get("title", ""),
                          "description": inner.get("description", ""),
                          "hashtags": inner.get("hashtags") or [],
                          "scheduled_at": inner.get("scheduled_at"),
                          "why": (inner.get("extra") or {}).get("why", "")})
    view["plans"] = plans
    vid = payload.get("video_id")
    if vid is not None and str(vid).isdigit():
        video = ctx.db.get_video(int(vid))
        if video:
            view["video"] = {"id": video.id, "title_hint": video.title_hint,
                             "file_name": Path(video.path).name,
                             "summary_he": (video.analysis or {}).get("summary_he", "")}
    return view


def _imported(post: dict) -> bool:
    """A post imported from the old studio's history (old news, never announced)."""
    return ((post.get("plan") or {}).get("extra") or {}).get("source") == "studio"


class TelegramNotifier:
    """The Telegram channel for one agency (``ctx`` = staff Context: settings + db)."""

    def __init__(self, ctx, *, token: str | None = None,
                 transport: Callable[..., tuple[int, dict]] | None = None,
                 run_job: Callable[..., Any] | None = None):
        self.ctx = ctx
        self.settings: Settings = ctx.settings
        self.db = ctx.db
        self._token = token
        self._transport = transport or self._http
        self._run_job = run_job or self._thread_job
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = "stopped"
        self._key: bytes | None = None

    # ------------------------------------------------------------------ config / state
    @property
    def token(self) -> str | None:
        return self._token or secret("TELEGRAM_BOT_TOKEN")

    def enabled(self) -> bool:
        return self.settings.get("notify.telegram.enabled", False) is True

    def configured(self) -> bool:
        return bool(self.token)

    def chat_id(self) -> int | None:
        """The paired private chat: kv (written at pairing) wins, else config."""
        rec = self.db.kv_get(CHAT_KV)
        value = rec.get("chat_id") if isinstance(rec, dict) else None
        if not isinstance(rec, dict):
            value = self.settings.get("notify.telegram.chat_id")
        text = str(value or "").strip()
        return int(text) if text.lstrip("-").isdigit() and int(text) != 0 else None

    def paired(self) -> bool:
        return self.chat_id() is not None

    def _chat_record(self) -> dict:
        rec = self.db.kv_get(CHAT_KV)
        return rec if isinstance(rec, dict) else {}

    def db_id(self) -> str:
        """A random id of THIS database (kv), so buttons die with the database they came
        from (a reset DB re-uses approval numbers)."""
        with self._lock:
            value = self.db.kv_get(DB_ID_KV)
            if not isinstance(value, str) or len(value) < 16:
                value = pysecrets.token_hex(16)
                self.db.kv_set(DB_ID_KV, value)
            return value

    def bind_of(self, approval: Approval) -> str:
        """What a button is bound to besides the chat: this database + this approval."""
        return f"{self.db_id()}|{approval.created_at or ''}"

    def callback_data(self, action: str, approval: Approval) -> str:
        """The signed ``callback_data`` of one button of ``approval``'s card."""
        return sign_callback(self.key(), action, int(approval.id), self.chat_id(),
                             self.bind_of(approval))

    def key(self) -> bytes:
        if self._key is None:
            self._key = hmac_key(self.settings)
        return self._key

    @property
    def state(self) -> str:
        return self._state

    def _set_state(self, state: str, message_he: str | None = None) -> None:
        if state == self._state and message_he is None:
            return
        self._state = state
        try:
            self.db.kv_set(STATE_KV, {"state": state, "message_he": message_he or
                                      STATES_HE.get(state, ""), "ts": time.time()})
        except Exception:  # pragma: no cover - state is informational
            pass

    def status(self) -> dict:
        """``{enabled, connected, paired, paired_name, paired_at, state, message_he}`` —
        never the token."""
        if not self.enabled():
            state = "off"
        elif not self.configured():
            state = "not_configured"
        elif self._state in ("busy_by_old_studio", "bad_token", "error"):
            state = self._state
        elif not self.paired():
            state = "not_paired"
        elif self._thread is not None and self._thread.is_alive():
            state = "running"
        else:
            state = self._state if self._state != "running" else "stopped"
        rec = self._chat_record() if self.paired() else {}
        return {"enabled": self.enabled(), "connected": self.configured(),
                "paired": self.paired(), "paired_name": rec.get("name") or None,
                "paired_at": rec.get("paired_at") or None,
                "state": state, "message_he": STATES_HE.get(state, "")}

    def ready(self) -> bool:
        """May send now: enabled, token, paired, and the bot is not held by the old studio."""
        return self.enabled() and self.configured() and self.paired() \
            and self._state not in ("busy_by_old_studio", "bad_token")

    # ------------------------------------------------------------------ Bot API
    def _http(self, method: str, params: dict, files: dict | None, timeout: float
              ) -> tuple[int, dict]:
        """POST one Bot API method. Exceptions never carry the URL (it contains the token)."""
        import requests

        url = f"{API_BASE}/bot{self.token}/{method}"
        try:
            if files:
                data = {k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list))
                        else v for k, v in params.items()}
                resp = requests.post(url, data=data, files=files, timeout=(10, timeout))
            else:
                resp = requests.post(url, json=params, timeout=(10, timeout))
        except requests.RequestException as exc:
            raise TelegramError(method, f"network ({type(exc).__name__})") from None
        try:
            body = resp.json()
        except ValueError:
            body = {}
        return resp.status_code, body if isinstance(body, dict) else {}

    def call(self, method: str, params: dict | None = None, *, files: dict | None = None,
             timeout: float = 30.0) -> Any:
        """Call a Bot API method; returns ``result`` or raises :class:`TelegramError`."""
        if not self.token:
            raise TelegramError(method, "no bot token")
        params = dict(params or {})
        for attempt in range(2):
            status, body = self._transport(method, params, files, timeout)
            if body.get("ok"):
                return body.get("result")
            code = int(body.get("error_code") or status or 0)
            retry_after = (body.get("parameters") or {}).get("retry_after")
            if code == 429 and attempt == 0 and retry_after and float(retry_after) <= 10:
                time.sleep(float(retry_after))
                continue
            description = str(body.get("description") or f"HTTP {status}")[:200]
            raise TelegramError(method, description, code=code,
                                retry_after=float(retry_after) if retry_after else None)
        raise TelegramError(method, "rate limited", code=429)  # pragma: no cover

    def send_message(self, text: str, buttons: list | None = None, *, silent: bool = False
                     ) -> dict | None:
        chat = self.chat_id()
        if chat is None:
            return None
        params: dict = {"chat_id": chat, "text": text[:TEXT_LIMIT], "parse_mode": "HTML",
                        "link_preview_options": {"is_disabled": True},
                        "disable_notification": silent}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        return self.call("sendMessage", params)

    def _edit(self, message_id: int, text: str) -> None:
        chat = self.chat_id()
        if chat is None:
            return
        try:
            self.call("editMessageText", {"chat_id": chat, "message_id": int(message_id),
                                          "text": text[:TEXT_LIMIT], "parse_mode": "HTML",
                                          "link_preview_options": {"is_disabled": True},
                                          "reply_markup": {"inline_keyboard": []}})
        except TelegramError as exc:
            if "not modified" not in str(exc):
                log.info("telegram edit failed: %s", exc)

    def _answer(self, callback_id: str, text: str = "") -> None:
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})
        except TelegramError:
            pass

    # ------------------------------------------------------------------ pairing
    def _pair_hash(self, code: str) -> str:
        return hmac.new(self.key(), f"pair|{code}".encode(), hashlib.sha256).hexdigest()

    def new_pairing_code(self, ttl_s: int = PAIRING_TTL_S) -> str:
        """A fresh 6-digit code for the dashboard to show (only its HMAC is stored)."""
        code = f"{pysecrets.randbelow(10 ** 6):06d}"
        self.db.kv_set(PAIRING_KV, {"hash": self._pair_hash(code),
                                    "expires": time.time() + ttl_s, "fails": {}, "total": 0})
        return code

    def unpair(self) -> None:
        """Forget the paired chat (kv says "unpaired" even if config still has an id). Its
        old cards stop working at once (their buttons are bound to that chat)."""
        self.db.kv_set(CHAT_KV, {"chat_id": None})
        self.db.kv_set(CARDS_KV, {})
        self._save_config_chat("")

    def pairing_active(self) -> bool:
        """A pairing code was issued and has not expired / been used / been cancelled."""
        rec = self.db.kv_get(PAIRING_KV) or {}
        return isinstance(rec, dict) and bool(rec.get("hash")) \
            and time.time() <= float(rec.get("expires") or 0)

    def _save_config_chat(self, chat_id: str) -> None:
        try:
            from ..settings_store import update_config
            update_config(self.settings, {"notify": {"telegram": {"chat_id": chat_id}}})
        except Exception as exc:  # kv still holds it; config is a convenience copy
            log.info("could not save the Telegram chat in config: %s", type(exc).__name__)

    def _try_pair(self, msg: dict) -> None:
        """``/start <code>`` in a private chat pairs (or re-pairs) that chat.

        Wrong codes are counted per chat: after ``MAX_PAIR_FAILS`` that chat is ignored, and
        only ``MAX_PAIR_FAILS_TOTAL`` wrong codes from everyone together cancel the code. The
        replies never say whether a code is waiting (a stranger learns nothing); while a chat
        is already paired, strangers get no reply at all.
        """
        chat, frm = msg.get("chat") or {}, msg.get("from") or {}
        text = str(msg.get("text") or "").strip()
        if chat.get("type") != "private" or not text.startswith("/start"):
            return
        quiet = self.paired()  # never chat with strangers while someone is paired
        chat_key = str(chat.get("id"))
        reply = None
        chat_id = ""
        with self._lock:
            rec = self.db.kv_get(PAIRING_KV) or {}
            fails = rec.get("fails") if isinstance(rec, dict) \
                and isinstance(rec.get("fails"), dict) else {}
            code = text[len("/start"):].strip()
            if not isinstance(rec, dict) or not rec.get("hash") \
                    or time.time() > float(rec.get("expires") or 0):
                reply = PAIR_HELP_HE
            elif int(fails.get(chat_key) or 0) >= MAX_PAIR_FAILS:
                return  # this chat used up its tries: ignored silently
            elif not (code.isdigit() and len(code) == 6 and isinstance(chat.get("id"), int)
                      and frm.get("id") == chat.get("id")
                      and hmac.compare_digest(self._pair_hash(code), str(rec["hash"]))):
                fails = {**fails, chat_key: int(fails.get(chat_key) or 0) + 1}
                total = int(rec.get("total") or 0) + 1
                if total >= MAX_PAIR_FAILS_TOTAL:
                    self.db.kv_set(PAIRING_KV, {})
                    emit(self.db, "manager", "מישהו ניסה לנחש את קוד הצימוד לטלגרם, אז ביטלתי "
                         "אותו. צור קוד חדש בחיבורים ← טלגרם.", "warning")
                else:
                    self.db.kv_set(PAIRING_KV, {**rec, "fails": fails, "total": total})
                reply = PAIR_HELP_HE
            else:
                chat_id = str(int(chat["id"]))
                previous = self.chat_id()
                name = str(frm.get("username") or "").strip()
                name = f"@{name}" if name else str(frm.get("first_name") or "").strip()[:60]
                self.db.kv_set(PAIRING_KV, {})
                self.db.kv_set(CHAT_KV, {"chat_id": chat_id, "name": name or None,
                                         "paired_at": time.time()})
                if previous is not None and str(previous) != chat_id:
                    self.db.kv_set(CARDS_KV, {})  # the old chat's cards are dead; resend here
        if not chat_id:
            if reply and not quiet:
                self._reply(chat.get("id"), reply)
            return
        self._baseline_published()
        self._save_config_chat(chat_id)
        emit(self.db, "manager", "הטלגרם חובר — אישורים יגיעו גם לטלפון", "success")
        self._set_state("running")
        self.send_message("✅ מחובר! מעכשיו אשלח לך לכאן כל מה שמחכה לאישור שלך.\n\n"
                          + HELP_HE)

    def _reply(self, chat_id: Any, text: str) -> None:
        """Answer an unpaired private chat (pairing help only)."""
        if not isinstance(chat_id, int):
            return
        try:
            self.call("sendMessage", {"chat_id": chat_id, "text": text})
        except TelegramError:
            pass

    # ------------------------------------------------------------------ cards
    def _cards(self) -> dict:
        cards = self.db.kv_get(CARDS_KV) or {}
        return cards if isinstance(cards, dict) else {}

    def _set_card(self, aid: int, **fields) -> None:
        with self._lock:
            cards = self._cards()
            cards[str(aid)] = {**(cards.get(str(aid)) or {}), **fields}
            if len(cards) > 300:
                cards = dict(list(cards.items())[-300:])
            self.db.kv_set(CARDS_KV, cards)

    def _platform_live(self, platform: str) -> bool:
        try:
            from ..platforms import registry
            return bool(registry.get_adapter(platform, self.settings).is_live())
        except Exception:
            return False

    def live_map(self, view: dict | Approval) -> dict[str, bool]:
        """``{platform: really uploads now}`` for the plans of a publish approval."""
        if isinstance(view, Approval):
            view = _view_from_approval(self.ctx, view)
        return {str(p.get("platform", "")): self._platform_live(str(p.get("platform", "")))
                for p in view.get("plans") or []}

    def card_text(self, view: dict, live: dict[str, bool] | None = None) -> str:
        """The Hebrew card (HTML). No ids, no technical words. ``live`` = the per-platform
        real-upload state to show (default: now)."""
        kind = view.get("kind")
        video = view.get("video") or {}
        plans = view.get("plans") or []
        if kind == "spend":
            cost = float(view.get("cost_ils") or 0)
            reason = (view.get("payload") or {}).get("reason") or view.get("note") or ""
            lines = ["💸 <b>בקשת הוצאה</b>", esc(view.get("title", "")),
                     f"<b>{cost:,.0f} ₪</b>"]
            if reason:
                lines.append(esc(str(reason)[:300]))
            return "\n".join(lines)
        if kind != "publish":
            return f"📝 <b>מחכה לאישור שלך</b>\n{esc(view.get('title', ''))}"
        title = video.get("title_hint") or (plans[0].get("title") if plans else "") \
            or view.get("title", "")
        lines = ["🎬 <b>מחכה לאישור שלך</b>", f"<b>{esc(title)}</b>"]
        if video.get("summary_he"):
            lines.append(esc(str(video["summary_he"])[:250]))
        lines += ["", "יעלה ל:"]
        if live is None:
            live = {p.get("platform", ""): self._platform_live(p.get("platform", ""))
                    for p in plans}
        for plan in sorted(plans, key=lambda p: str(p.get("scheduled_at") or "")):
            platform = plan.get("platform", "")
            mark = "" if live.get(platform) else " 🧪"
            lines.append(f"{PLATFORM_ICON.get(platform, '•')} {PLATFORM_HE.get(platform, platform)}"
                         f" · {when_he(self.settings, plan.get('scheduled_at'))}{mark}")
            if plan.get("title"):
                lines.append(f"   «{esc(str(plan['title'])[:90])}»")
        if plans and not any(live.values()):
            lines += ["", "🧪 מצב אימון — שום דבר לא יעלה באמת."]
        elif not all(live.values()):
            lines += ["", "🧪 = מצב אימון בפלטפורמה הזו."]
        return "\n".join(lines)

    def _buttons(self, approval: Approval) -> list:
        return [[{"text": BTN_APPROVE, "callback_data": self.callback_data("A", approval)},
                 {"text": BTN_LATER, "callback_data": self.callback_data("L", approval)}]]

    def send_approval_card(self, approval_view: dict | Approval, *, force: bool = False,
                           refresh: bool = False) -> bool:
        """Send one approval card (idempotent per approval). True when a card was sent.

        ``approval_view`` is the dashboard's ``approval_view(ctx, a)`` dict (or an Approval).
        Nothing is sent when the channel is off, unpaired, or busy with the old studio.
        """
        if not self.ready():
            return False
        view = _view_from_approval(self.ctx, approval_view) \
            if isinstance(approval_view, Approval) else dict(approval_view or {})
        aid = view.get("id")
        if aid is None or view.get("status", "pending") != "pending" \
                or view.get("kind") not in CARD_KINDS:
            return False
        if not force and str(aid) in self._cards():
            return False
        approval = self.db.get_approval(int(aid))
        if approval is None:
            return False
        if view.get("kind") == "publish" and not refresh:
            self._send_preview(view)
            self._send_full_text(view)
        live = self.live_map(view) if view.get("kind") == "publish" else {}
        text = self.card_text(view, live)
        if refresh:
            text = "🔄 <b>עודכן</b> — " + text
        try:
            msg = self.send_message(text, self._buttons(approval))
        except TelegramError as exc:
            self._note_error(exc)
            return False
        if not msg:
            return False
        self._set_card(int(aid), message_id=msg.get("message_id"), chat_id=self.chat_id(),
                       snoozed=False, closed=False, ts=time.time(), live=live)
        return True

    def _refresh_card(self, approval: Approval, old_message_id: Any = None) -> bool:
        """The training / real-upload state changed since the card was sent: the old card
        loses its buttons and a fresh one (with the true state) is sent."""
        if old_message_id:
            self._edit(old_message_id, f"{esc(approval.title)}\n⚠️ המצב השתנה — יש כרטיס "
                                       "מעודכן למטה.")
        return self.send_approval_card(approval, force=True, refresh=True)

    def full_text(self, view: dict) -> list[str]:
        """Every word that will be published, per platform (≤3900-char parts, whole lines)."""
        lines = ["📄 <b>הטקסט המלא שיעלה</b>"]
        for plan in sorted(view.get("plans") or [],
                           key=lambda p: str(p.get("scheduled_at") or "")):
            platform = plan.get("platform", "")
            lines.append(f"\n{PLATFORM_ICON.get(platform, '•')} "
                         f"<b>{PLATFORM_HE.get(platform, platform)}</b>")
            if plan.get("title"):
                lines.append(f"<b>{esc(plan['title'])}</b>")
            for para in str(plan.get("description") or "").split("\n"):
                lines.append(esc(para))
            tags = plan.get("hashtags") or []
            if tags:
                lines.append(esc(" ".join(t if str(t).startswith("#") else f"#{t}"
                                          for t in tags)))
        parts, current = [], ""
        for line in lines:
            if current and len(current) + len(line) + 1 > TEXT_LIMIT:
                parts.append(current)
                current = ""
            current += line[:TEXT_LIMIT] + "\n"
        if current.strip():
            parts.append(current)
        return parts

    def _send_full_text(self, view: dict) -> None:
        if self.settings.get("notify.telegram.full_text", True) is False:
            return
        if not any(p.get("description") or p.get("hashtags") for p in view.get("plans") or []):
            return  # the card itself already shows the titles
        for part in self.full_text(view):
            self.send_message(part, silent=True)

    def _send_preview(self, view: dict) -> None:
        if self.settings.get("notify.telegram.send_preview", True) is False:
            return
        vid = (view.get("video") or {}).get("id") or (view.get("payload") or {}).get("video_id")
        video = self.db.get_video(int(vid)) if vid is not None and str(vid).isdigit() else None
        preview = (video.meta or {}).get("preview") if video else None
        if not preview or not Path(preview).is_file() \
                or Path(preview).stat().st_size > PREVIEW_MAX_BYTES:
            return
        try:
            with open(preview, "rb") as fh:
                self.call("sendVideo", {"chat_id": self.chat_id(), "supports_streaming": True,
                                        "protect_content": True, "disable_notification": True},
                          files={"video": (Path(preview).name, fh, "video/mp4")}, timeout=300)
        except (TelegramError, OSError) as exc:  # the card still goes out without it
            log.info("telegram preview not sent: %s", type(exc).__name__)

    def send_published(self, post: dict | int) -> bool:
        """"✅ עלה!" with the real link for a published, non-training post. True when sent."""
        if not self.ready():
            return False
        if isinstance(post, int) or (isinstance(post, str) and str(post).isdigit()):
            post = self.db.get_post(int(post)) or {}
        if not isinstance(post, dict) or post.get("status") != "published" or _imported(post):
            return False
        result = post.get("result") or {}
        url = post.get("url") or result.get("url")
        if result.get("dry_run") or not url or not str(url).startswith("https://"):
            return False
        with self._lock:
            sent = self.db.kv_get(SENT_KV) or []
            if post.get("id") in sent:
                return False
        platform = post.get("platform", "")
        title = (post.get("plan") or {}).get("title") or ""
        text = f"✅ עלה ל{PLATFORM_HE.get(platform, platform)}!" + (
            f"\n<b>{esc(title[:120])}</b>" if title else "")
        try:
            self.send_message(text, [[{"text": "לצפייה ▶️", "url": str(url)}]], silent=True)
        except TelegramError as exc:
            self._note_error(exc)
            return False
        with self._lock:
            sent = list(self.db.kv_get(SENT_KV) or [])
            sent.append(post.get("id"))
            self.db.kv_set(SENT_KV, sent[-500:])
        return True

    # ------------------------------------------------------------------ updates
    def handle_update(self, update: dict) -> None:
        """Dispatch one update. Anything not from the paired private chat is ignored."""
        try:
            if "callback_query" in update:
                self._on_callback(update["callback_query"])
            elif "message" in update:
                self._on_message(update["message"])
        except Exception as exc:  # one bad update must never stop the bot
            log.warning("telegram update failed: %s", type(exc).__name__)

    def _is_owner(self, chat: dict, frm: dict) -> bool:
        owner = self.chat_id()
        return owner is not None and chat.get("type") == "private" \
            and chat.get("id") == owner and frm.get("id") == owner

    def _on_message(self, msg: dict) -> None:
        chat, frm = msg.get("chat") or {}, msg.get("from") or {}
        starts = str(msg.get("text") or "").strip().startswith("/start ")
        if not self.paired() or (starts and self.pairing_active()):
            self._try_pair(msg)  # pairing, or re-pairing to a new chat with a fresh code
            return
        if not self._is_owner(chat, frm):
            return
        text = str(msg.get("text") or "").strip().split(" ")[0].lower()
        if text in ("/status", "מצב", "מה המצב?", "מה המצב"):
            waiting = len(self.db.list_approvals("pending"))
            self.send_message("הכל רץ 👌" + (f" יש {waiting} דברים שמחכים לך." if waiting
                                            else " אין כרגע משהו שמחכה לך."))
        else:
            self.send_message(HELP_HE)

    def _on_callback(self, cq: dict) -> None:
        msg = cq.get("message") or {}
        chat, frm = msg.get("chat") or {}, cq.get("from") or {}
        if not self._is_owner(chat, frm):
            return  # wrong chat or a stranger: ignore completely
        claimed = parse_callback(cq.get("data"))
        approval = self.db.get_approval(claimed[1]) if claimed else None
        parsed = verify_callback(self.key(), cq.get("data"), self.chat_id(),
                                 self.bind_of(approval)) if approval is not None else None
        if parsed is None:
            self._answer(cq.get("id", ""), "הכפתור הזה לא תקף")
            return
        action, aid = parsed
        card = self._cards().get(str(aid)) or {}
        if card.get("message_id") and msg.get("message_id") \
                and str(card["message_id"]) != str(msg["message_id"]):
            self._answer(cq.get("id", ""), "הכפתור הזה ישן — השתמש בכרטיס האחרון")
            return
        message_id = msg.get("message_id") or card.get("message_id")
        if action == "L":
            self._answer(cq.get("id", ""), "בסדר, זה מחכה לך")
            if approval.status == "pending" and message_id:
                self._edit(message_id, self.card_text(_view_from_approval(self.ctx, approval))
                           + "\n\n⏸ מחכה לך בלוח הבקרה.")
            self._set_card(aid, snoozed=True, closed=True)
            return
        self._approve(cq.get("id", ""), approval, message_id, card)

    def _approve(self, callback_id: str, approval: Approval, message_id: Any,
                 card: dict | None = None) -> None:
        """Exactly the dashboard's approve: decide (pending) or resume (approved), then execute.

        A publish card is obeyed only while every platform's training / real-upload state is
        what the card showed; otherwise a fresh card is sent and nothing happens.
        """
        aid = int(approval.id)
        if approval.kind not in CARD_KINDS:
            self._answer(callback_id, "את זה מאשרים בלוח הבקרה")
            return
        if approval.kind == "publish" and approval.status in ("pending", "approved"):
            shown = (card or {}).get("live")
            if not isinstance(shown, dict) or shown != self.live_map(approval):
                self._answer(callback_id, CHANGED_HE)
                self._refresh_card(approval, message_id)
                return
        if approval.status == "pending":
            try:
                approval = self.db.decide_approval(aid, True, "אושר בטלגרם")
            except (KeyError, ValueError):
                current = self.db.get_approval(aid)
                if current is None or current.status != "approved":
                    self._answer(callback_id, "זה כבר טופל")
                    self._set_card(aid, closed=True)
                    return
                approval = current
            emit(self.db, "owner", f"הבעלים אישר בטלגרם: {approval.title}", "success")
        elif approval.status != "approved":
            self._answer(callback_id, "זה כבר טופל")
            self._set_card(aid, closed=True)
            if message_id:
                self._edit(message_id, f"✔️ {esc(approval.title)} — כבר טופל.")
            return
        self._answer(callback_id, "יאללה, מעלה! 🚀")
        if message_id:
            self._edit(message_id, f"✅ <b>אושר</b> — {esc(approval.title)}\nמטפל בזה עכשיו…")
        self._set_card(aid, closed=True)
        self._run_job(self._execute, aid)

    def _execute(self, aid: int) -> dict:
        """pipeline.execute_approval, then a short honest report (+ links of what went up)."""
        try:
            from ..pipeline import execute_approval
            result = execute_approval(self.ctx, aid)
        except Exception as exc:
            emit(self.db, "manager", f"ביצוע אישור נכשל: {type(exc).__name__}: {exc}", "error")
            result = {"ok": False, "error": type(exc).__name__}
        approval = self.db.get_approval(aid)
        if approval is not None and approval.kind == "publish":
            for pid in (approval.payload or {}).get("post_ids") or []:
                try:
                    self.send_published(int(pid))
                except Exception:  # pragma: no cover - send_published guards itself
                    pass
            failed = [r for r in result.get("results") or [] if not r.get("ok")]
            if failed:
                names = ", ".join(PLATFORM_HE.get(r.get("platform", ""), r.get("platform", ""))
                                  for r in failed)
                self._safe_send(f"⚠️ לא הצליח לעלות ל: {esc(names)}. הפרטים בלוח הבקרה.")
            elif result.get("scheduled_post_ids") and not result.get("results"):
                self._safe_send("⏰ מתוזמן. אשלח לך קישור כשזה עולה.", silent=True)
        elif not result.get("ok"):
            self._safe_send("⚠️ זה לא בוצע. הפרטים בלוח הבקרה.")
        return result

    def _safe_send(self, text: str, silent: bool = False) -> None:
        try:
            self.send_message(text, silent=silent)
        except TelegramError as exc:
            self._note_error(exc)

    def _note_error(self, exc: TelegramError) -> None:
        if exc.code == 409:
            self._set_state("busy_by_old_studio")
        elif exc.code in (401, 404):
            self._set_state("bad_token")
        log.info("telegram: %s", exc)

    # ------------------------------------------------------------------ polling
    def poll_once(self, timeout: int | None = None) -> int:
        """One ``getUpdates`` long poll; handles the updates. Returns how many (-1 on error).

        409 Conflict (the old studio is polling the same bot) -> ``busy_by_old_studio``.
        """
        long_poll = int(self.settings.get("notify.telegram.poll_timeout_s", LONG_POLL_S)
                        if timeout is None else timeout)
        offset = int(self.db.kv_get(OFFSET_KV) or 0)
        try:
            updates = self.call("getUpdates", {"offset": offset, "timeout": long_poll,
                                               "allowed_updates": ["message", "callback_query"]},
                                timeout=long_poll + 15)
        except TelegramError as exc:
            if exc.code == 409:
                self._set_state("busy_by_old_studio")
            elif exc.code in (401, 404):
                self._set_state("bad_token")
            else:
                self._set_state("error")
            log.info("telegram: %s", exc)
            return -1
        self._set_state("running" if self.paired() else "not_paired")
        count = 0
        for update in updates or []:
            if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                continue
            self.db.kv_set(OFFSET_KV, update["update_id"] + 1)  # before handling: never twice
            self.handle_update(update)
            count += 1
        return count

    def sync(self) -> dict:
        """Send cards for waiting approvals, close stale cards, announce new published posts."""
        out = {"cards": 0, "closed": 0, "published": 0}
        if not self.ready():
            return out
        cards = self._cards()
        for approval in self.db.list_approvals("pending")[:50]:
            if out["cards"] >= MAX_CARDS_PER_SYNC:
                break
            if approval.kind in CARD_KINDS and str(approval.id) not in cards \
                    and self.send_approval_card(approval):
                out["cards"] += 1
        for aid, card in list(self._cards().items()):
            if card.get("closed") or not str(aid).isdigit():
                continue
            approval = self.db.get_approval(int(aid))
            if approval is not None and approval.status == "pending" \
                    and approval.kind == "publish" and isinstance(card.get("live"), dict) \
                    and card["live"] != self.live_map(approval):
                self._refresh_card(approval, card.get("message_id"))  # a switch changed
                continue
            if approval is None or approval.status != "pending":
                if card.get("message_id"):
                    done = "✔️ טופל בלוח הבקרה." if approval is not None else "✔️ טופל."
                    self._edit(card["message_id"], f"{esc(approval.title if approval else '')}"
                                                   f"\n{done}")
                self._set_card(int(aid), closed=True)
                out["closed"] += 1
        sent = self.db.kv_get(SENT_KV)
        if sent is None:  # first run: what is already live is history, not news
            self._baseline_published()
            return out
        sent = set(sent)
        for post in self.db.list_posts()[:200]:
            if post.get("status") == "published" and post.get("id") not in sent \
                    and not _imported(post) and self.send_published(post):
                out["published"] += 1
        return out

    def _baseline_published(self) -> None:
        with self._lock:
            ids = [p["id"] for p in self.db.list_posts() if p.get("status") == "published"]
            self.db.kv_set(SENT_KV, ids[:500])

    def run(self, stop: threading.Event | None = None) -> None:
        """The background loop: long poll + sync, with slow retries while busy / failing."""
        stop = stop or self._stop
        while not stop.is_set():
            if not (self.enabled() and self.configured()):
                self._set_state("not_configured" if self.enabled() else "off")
                stop.wait(ERROR_BACKOFF_S)
                continue
            handled = self.poll_once()
            if handled >= 0:
                try:
                    self.sync()
                except Exception as exc:
                    log.warning("telegram sync failed: %s", type(exc).__name__)
                continue
            stop.wait(CONFLICT_BACKOFF_S if self._state == "busy_by_old_studio"
                      else ERROR_BACKOFF_S)
        self._set_state("stopped")

    def start(self) -> bool:
        """Start the polling thread (idempotent). False when off or without a token."""
        if not (self.enabled() and self.configured()):
            self._set_state("not_configured" if self.enabled() else "off")
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self.run, args=(self._stop,), daemon=True,
                                            name="telegram-bot")
            self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None

    @staticmethod
    def _thread_job(fn: Callable, *args) -> None:
        threading.Thread(target=fn, args=args, daemon=True, name="telegram-job").start()


# ------------------------------------------------------------------------------- singleton
_instance_lock = threading.Lock()
_instance: TelegramNotifier | None = None


def get_notifier() -> TelegramNotifier | None:
    """The running notifier of this process, if any."""
    return _instance


def start_telegram(ctx) -> TelegramNotifier | None:
    """Start the Telegram channel for ``ctx`` (dashboard startup). None when off / no token."""
    global _instance
    with _instance_lock:
        if _instance is None or _instance.ctx is not ctx:
            if _instance is not None:
                _instance.stop()
            _instance = TelegramNotifier(ctx)
        notifier = _instance
    return notifier if notifier.start() else None


def stop_telegram() -> None:
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.stop()
        _instance = None


def telegram_status(ctx) -> dict:
    """``{enabled, connected, paired, state, message_he}`` for the connections page."""
    notifier = _instance if _instance is not None and _instance.ctx is ctx \
        else TelegramNotifier(ctx)
    status = notifier.status()
    if notifier is not _instance:  # another process may run the bot: use its last state
        saved = ctx.db.kv_get(STATE_KV) or {}
        if status["state"] in ("stopped",) and isinstance(saved, dict) \
                and saved.get("state") in STATES_HE and time.time() - float(
                    saved.get("ts") or 0) < 300:
            status.update(state=saved["state"], message_he=STATES_HE[saved["state"]])
    return status


def new_pairing_code(ctx, *, unpair: bool = False) -> str:
    """A fresh 6-digit pairing code for the dashboard to show once. ``unpair`` first forgets
    the paired chat (re-pairing: the old phone loses its buttons right away)."""
    notifier = _instance if _instance is not None and _instance.ctx is ctx \
        else TelegramNotifier(ctx)
    if unpair and notifier.paired():
        notifier.unpair()
        emit(ctx.db, "owner", "הטלגרם הקודם נותק — מחכה לצימוד החדש", "warning")
    return notifier.new_pairing_code()


def unpair(ctx) -> None:
    """Forget the paired Telegram chat (e.g. when the bot is disconnected)."""
    notifier = _instance if _instance is not None and _instance.ctx is ctx \
        else TelegramNotifier(ctx)
    if notifier.paired():
        notifier.unpair()
    notifier.db.kv_set(PAIRING_KV, {})


def notify_approval(ctx, approval: Approval | dict) -> bool:
    """Best-effort: send a card now if the channel is running (else the loop's sync does it)."""
    notifier = _instance
    if notifier is None or notifier.ctx is not ctx:
        return False
    try:
        return notifier.send_approval_card(approval)
    except Exception:
        return False


def notify_published(ctx, post: dict | int) -> bool:
    """Best-effort "it is live" message (no-op when the channel is not running)."""
    notifier = _instance
    if notifier is None or notifier.ctx is not ctx:
        return False
    try:
        return notifier.send_published(post)
    except Exception:
        return False
