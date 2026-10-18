"""Telegram approvals: signed buttons, pairing, only the paired chat is obeyed, 409 from the old
studio's poller, approvals go through the pipeline exactly like the dashboard. No network."""
from __future__ import annotations

import json
import logging

import pytest

from agency.models import Approval, PostPlan
from agency.notify import telegram as tg
from agency.notify.telegram import (BTN_APPROVE, BTN_LATER, TelegramError, TelegramNotifier,
                                    sign_callback, verify_callback)

TOKEN = "123456:AA-SECRET-bot-token-abcdef"
OWNER = 424242
STRANGER = 777


class FakeBot:
    """Transport stand-in: records calls, answers like the Bot API."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[list[dict]] = []
        self.fail: dict[str, tuple[int, dict]] = {}
        self.next_id = 100

    def __call__(self, method, params, files, timeout):
        assert timeout and timeout > 0
        self.calls.append((method, json.loads(json.dumps(params, default=str))))
        if method in self.fail:
            return self.fail[method]
        if method == "getUpdates":
            return 200, {"ok": True, "result": self.updates.pop(0) if self.updates else []}
        if method in ("sendMessage", "sendVideo"):
            self.next_id += 1
            return 200, {"ok": True, "result": {"message_id": self.next_id}}
        return 200, {"ok": True, "result": True}

    def sent(self, method="sendMessage"):
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def bot(ctx, monkeypatch):
    monkeypatch.setenv("AGENCY_KEYRING", "off")
    ctx.settings.raw["notify"] = {"telegram": {"enabled": True}}
    fake = FakeBot()
    notifier = TelegramNotifier(ctx, token=TOKEN, transport=fake,
                                run_job=lambda fn, *args: fn(*args))
    return notifier, fake


def pair(notifier, fake):
    code = notifier.new_pairing_code()
    notifier.handle_update({"update_id": 1, "message": {
        "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER},
        "text": f"/start {code}"}})
    assert notifier.chat_id() == OWNER
    fake.calls.clear()


def callback(data, chat=OWNER, frm=OWNER, message_id=101):
    return {"update_id": 9, "callback_query": {
        "id": "cb1", "data": data, "from": {"id": frm},
        "message": {"message_id": message_id, "chat": {"id": chat, "type": "private"}}}}


def card_button(notifier, fake, aid, action="A"):
    """Send the approval's card and return (callback_data, message_id) of one of its buttons
    (buttons are bound to this database and this approval, so they come from a real card)."""
    assert notifier.send_approval_card(notifier.db.get_approval(aid), force=True)
    msg = [p for p in fake.sent() if p.get("reply_markup")][-1]
    buttons = msg["reply_markup"]["inline_keyboard"][0]
    data = buttons[0 if action == "A" else 1]["callback_data"]
    return data, notifier._cards()[str(aid)]["message_id"]


def make_publish_approval(ctx, tiny_video) -> tuple[int, int]:
    vid = ctx.db.add_video(str(tiny_video), "דיגל 3K בסגנון סרט")
    plan = PostPlan(platform="youtube", variant_path=str(tiny_video), title="דיגל 3K",
                    description="d")
    pid = ctx.db.add_post(vid, plan, status="awaiting_approval")
    aid = ctx.db.add_approval(Approval(id=None, kind="publish", title="פרסום דיגל",
                                       requested_by="manager",
                                       payload={"video_id": vid, "post_ids": [pid],
                                                "plans": [plan.to_dict()]}))
    return aid, pid


# ------------------------------------------------------------------------------ signatures
def test_signed_callbacks_accept_and_reject():
    key = b"k" * 32
    data = sign_callback(key, "A", 17, OWNER)
    assert len(data.encode()) <= 64
    assert verify_callback(key, data, OWNER) == ("A", 17)
    assert verify_callback(key, data, STRANGER) is None          # bound to the paired chat
    assert verify_callback(b"x" * 32, data, OWNER) is None       # other key
    assert verify_callback(key, data.replace("|17|", "|18|"), OWNER) is None
    assert verify_callback(key, data.replace("|A|", "|L|"), OWNER) is None
    assert verify_callback(key, data[:-1] + ("0" if data[-1] != "0" else "1"), OWNER) is None
    assert verify_callback(key, "v1|X|17|abc", OWNER) is None
    assert verify_callback(key, None, OWNER) is None
    with pytest.raises(ValueError):
        sign_callback(key, "Z", 1, OWNER)


def test_hmac_key_generated_once_and_kept_out_of_db(ctx, monkeypatch):
    monkeypatch.setenv("AGENCY_KEYRING", "off")
    first = tg.hmac_key(ctx.settings)
    assert len(first) == 64 and tg.hmac_key(ctx.settings) == first
    path = ctx.settings.data_dir / "secrets" / tg.HMAC_FILE
    assert path.is_file()
    import sqlite3
    dump = "\n".join(sqlite3.connect(ctx.settings.db_path).iterdump())
    assert first.decode() not in dump


# ------------------------------------------------------------------------------ pairing
def test_pairing_with_code(bot, ctx):
    notifier, fake = bot
    assert notifier.status()["state"] == "not_paired"
    code = notifier.new_pairing_code()
    assert len(code) == 6 and code.isdigit()
    assert code not in json.dumps(ctx.db.kv_get(tg.PAIRING_KV))  # only its HMAC is stored
    group = {"chat": {"id": -100, "type": "group"}, "from": {"id": OWNER},
             "text": f"/start {code}"}
    notifier.handle_update({"update_id": 1, "message": group})
    assert notifier.chat_id() is None
    wrong = "000000" if code != "000000" else "111111"
    notifier.handle_update({"update_id": 2, "message": {
        "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER},
        "text": f"/start {wrong}"}})
    # the reply never says whether a code is waiting (a stranger learns nothing)
    assert notifier.chat_id() is None and fake.sent()[-1]["text"] == tg.PAIR_HELP_HE
    notifier.handle_update({"update_id": 3, "message": {
        "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER},
        "text": f"/start {code}"}})
    assert notifier.chat_id() == OWNER
    assert ctx.settings.get("notify.telegram.chat_id") == str(OWNER)  # saved in config
    assert "מחובר" in fake.sent()[-1]["text"]
    # the code is single-use
    notifier.unpair()
    notifier.handle_update({"update_id": 4, "message": {
        "chat": {"id": STRANGER, "type": "private"}, "from": {"id": STRANGER},
        "text": f"/start {code}"}})
    assert notifier.chat_id() is None


def test_pairing_failures_count_per_chat(bot, ctx):
    notifier, fake = bot
    code = notifier.new_pairing_code()
    for i in range(tg.MAX_PAIR_FAILS + 3):  # a stranger guessing cannot cancel our code
        notifier.handle_update({"update_id": i, "message": {
            "chat": {"id": STRANGER, "type": "private"}, "from": {"id": STRANGER},
            "text": "/start 12345x"}})
    assert len(fake.sent()) == tg.MAX_PAIR_FAILS  # then that chat is ignored silently
    assert notifier.pairing_active() and notifier.chat_id() is None
    notifier.handle_update({"update_id": 50, "message": {
        "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER},
        "text": f"/start {code}"}})
    assert notifier.chat_id() == OWNER
    # many chats together still cancel it (bounded guessing), and the owner is told
    notifier.unpair()
    notifier.new_pairing_code()
    for i in range(tg.MAX_PAIR_FAILS_TOTAL):
        chat = 10_000 + i // tg.MAX_PAIR_FAILS
        notifier.handle_update({"update_id": 100 + i, "message": {
            "chat": {"id": chat, "type": "private"}, "from": {"id": chat},
            "text": "/start 000000" if code != "000000" else "/start 111111"}})
    assert ctx.db.kv_get(tg.PAIRING_KV) == {} and not notifier.pairing_active()
    assert any("לנחש את קוד הצימוד" in e["message"] for e in ctx.db.list_events())


# ------------------------------------------------------------------------------ cards
def test_approval_card_text_buttons_and_idempotency(bot, ctx, tiny_video):
    notifier, fake = bot
    aid, _pid = make_publish_approval(ctx, tiny_video)
    approval = ctx.db.get_approval(aid)
    assert notifier.send_approval_card(approval) is False  # not paired yet: nothing sent
    pair(notifier, fake)
    assert notifier.send_approval_card(approval) is True
    msg = fake.sent()[-1]
    assert msg["chat_id"] == OWNER and msg["parse_mode"] == "HTML"
    assert "דיגל 3K בסגנון סרט" in msg["text"] and "יוטיוב" in msg["text"]
    assert f"#{aid}" not in msg["text"] and "dry" not in msg["text"].lower()
    assert "מצב אימון" in msg["text"]
    buttons = msg["reply_markup"]["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == [BTN_APPROVE, BTN_LATER]
    key, bind = notifier.key(), notifier.bind_of(approval)
    assert verify_callback(key, buttons[0]["callback_data"], OWNER, bind) == ("A", aid)
    assert verify_callback(key, buttons[1]["callback_data"], OWNER, bind) == ("L", aid)
    assert verify_callback(key, buttons[0]["callback_data"], OWNER) is None  # bound
    assert notifier.send_approval_card(approval) is False  # one card per approval
    full, card = fake.sent()
    assert "הטקסט המלא שיעלה" in full["text"] and full["disable_notification"] is True
    assert card is msg and "reply_markup" not in full


def test_reply_approvals_stay_in_the_dashboard(bot, ctx):
    notifier, fake = bot
    pair(notifier, fake)
    aid = ctx.db.add_approval(Approval(id=None, kind="reply", title="תשובה לתגובה",
                                       requested_by="community",
                                       payload={"text": "תודה אחי"}))
    assert notifier.send_approval_card(ctx.db.get_approval(aid)) is False
    assert notifier.sync()["cards"] == 0 and fake.sent() == []
    notifier.handle_update(callback(notifier.callback_data("A", ctx.db.get_approval(aid))))
    assert ctx.db.get_approval(aid).status == "pending"
    assert [p["text"] for m, p in fake.calls if m == "answerCallbackQuery"] == [
        "את זה מאשרים בלוח הבקרה"]


def test_spend_card_shows_amount(bot, ctx):
    notifier, fake = bot
    pair(notifier, fake)
    aid = ctx.db.add_approval(Approval(id=None, kind="spend", title="קידום בטיקטוק",
                                       cost_ils=50, requested_by="cfo",
                                       payload={"reason": "הקליפ הכי חזק השבוע"}))
    assert notifier.send_approval_card(ctx.db.get_approval(aid))
    text = fake.sent()[-1]["text"]
    assert "50 ₪" in text and "הקליפ הכי חזק השבוע" in text


# ------------------------------------------------------------------------------ callbacks
def test_approve_button_runs_the_pipeline(bot, ctx, tiny_video):
    notifier, fake = bot
    pair(notifier, fake)
    aid, pid = make_publish_approval(ctx, tiny_video)
    data, message_id = card_button(notifier, fake, aid)
    fake.calls.clear()
    notifier.handle_update(callback(data, message_id=message_id))
    approval = ctx.db.get_approval(aid)
    assert approval.status == "executed" and approval.note == "אושר בטלגרם"
    post = ctx.db.get_post(pid)
    assert post["status"] == "published" and post["result"]["dry_run"] is True
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "יאללה, מעלה! 🚀"}) \
        in fake.calls
    assert any(m == "editMessageText" for m, _ in fake.calls)
    assert not [p for p in fake.sent() if "עלה ל" in p["text"]]  # training mode: no "live!"
    # a second tap is harmless
    fake.calls.clear()
    notifier.handle_update(callback(data, message_id=message_id))
    assert ctx.db.get_approval(aid).status == "executed"
    assert ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "זה כבר טופל"}) \
        in fake.calls


def test_wrong_chat_and_forged_buttons_are_ignored(bot, ctx, tiny_video):
    notifier, fake = bot
    pair(notifier, fake)
    aid, _pid = make_publish_approval(ctx, tiny_video)
    good = sign_callback(notifier.key(), "A", aid, OWNER)
    notifier.handle_update(callback(good, chat=STRANGER, frm=STRANGER))  # someone else
    notifier.handle_update(callback(good, chat=OWNER, frm=STRANGER))     # group-ish mix
    assert ctx.db.get_approval(aid).status == "pending" and fake.calls == []
    forged = sign_callback(b"wrong-key" * 4, "A", aid, OWNER)
    notifier.handle_update(callback(forged))
    other_chat = sign_callback(notifier.key(), "A", aid, STRANGER)
    notifier.handle_update(callback(other_chat))
    assert ctx.db.get_approval(aid).status == "pending"
    answers = [p["text"] for m, p in fake.calls if m == "answerCallbackQuery"]
    assert answers == ["הכפתור הזה לא תקף", "הכפתור הזה לא תקף"]


def test_not_now_keeps_the_approval_waiting(bot, ctx, tiny_video):
    notifier, fake = bot
    pair(notifier, fake)
    aid, _pid = make_publish_approval(ctx, tiny_video)
    data, message_id = card_button(notifier, fake, aid, "L")
    notifier.handle_update(callback(data, message_id=message_id))
    assert ctx.db.get_approval(aid).status == "pending"
    edit = next(p for m, p in fake.calls if m == "editMessageText")
    assert "מחכה לך בלוח הבקרה" in edit["text"] and edit["reply_markup"] == {
        "inline_keyboard": []}
    fake.calls.clear()
    assert notifier.sync()["cards"] == 0  # not re-sent after "not now"


def test_sync_sends_waiting_cards_and_closes_decided_ones(bot, ctx, tiny_video):
    notifier, fake = bot
    pair(notifier, fake)
    aid, _pid = make_publish_approval(ctx, tiny_video)
    assert notifier.sync()["cards"] == 1
    ctx.db.decide_approval(aid, False, "dashboard")
    out = notifier.sync()
    assert out["closed"] == 1 and "טופל בלוח הבקרה" in next(
        p for m, p in fake.calls if m == "editMessageText")["text"]


# ------------------------------------------------------------------------------ polling / 409
def test_poll_once_offsets_and_dispatch(bot, ctx):
    notifier, fake = bot
    code = notifier.new_pairing_code()
    fake.updates = [[{"update_id": 50, "message": {
        "chat": {"id": OWNER, "type": "private"}, "from": {"id": OWNER},
        "text": f"/start {code}"}}]]
    assert notifier.poll_once(timeout=1) == 1
    assert notifier.chat_id() == OWNER and ctx.db.kv_get(tg.OFFSET_KV) == 51
    params = next(p for m, p in fake.calls if m == "getUpdates")
    assert params["offset"] == 0 and params["timeout"] == 1
    assert params["allowed_updates"] == ["message", "callback_query"]
    notifier.poll_once(timeout=1)
    assert [p for m, p in fake.calls if m == "getUpdates"][-1]["offset"] == 51


def test_conflict_409_means_busy_by_old_studio(bot, ctx, tiny_video):
    notifier, fake = bot
    pair(notifier, fake)
    fake.fail["getUpdates"] = (409, {"ok": False, "error_code": 409, "description":
                                     "Conflict: terminated by other getUpdates request"})
    assert notifier.poll_once(timeout=1) == -1
    status = notifier.status()
    assert status["state"] == "busy_by_old_studio"
    assert status["message_he"].startswith("הבוט תפוס ע״י הסטודיו הישן")
    assert ctx.db.kv_get(tg.STATE_KV)["state"] == "busy_by_old_studio"
    aid, _pid = make_publish_approval(ctx, tiny_video)
    fake.calls.clear()
    assert notifier.send_approval_card(ctx.db.get_approval(aid)) is False  # dashboard only
    assert notifier.sync() == {"cards": 0, "closed": 0, "published": 0}
    assert fake.calls == []
    del fake.fail["getUpdates"]
    assert notifier.poll_once(timeout=1) == 0 and notifier.state == "running"


def test_token_never_in_errors_or_logs(ctx, monkeypatch, caplog):
    import requests

    monkeypatch.setenv("AGENCY_KEYRING", "off")
    ctx.settings.raw["notify"] = {"telegram": {"enabled": True}}

    def boom(url, **kwargs):
        raise requests.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(requests, "post", boom)
    caplog.set_level(logging.DEBUG)
    notifier = TelegramNotifier(ctx, token=TOKEN)
    with pytest.raises(TelegramError) as info:
        notifier.call("getMe")
    assert TOKEN not in str(info.value) and "api.telegram.org" not in str(info.value)
    assert notifier.poll_once(timeout=1) == -1 and notifier.state == "error"
    assert TOKEN not in caplog.text
    assert TOKEN not in json.dumps(notifier.status())


def test_send_published_only_for_real_links(bot, ctx, tiny_video):
    from agency.models import PostResult

    notifier, fake = bot
    pair(notifier, fake)
    vid = ctx.db.add_video(str(tiny_video), "t")
    plan = PostPlan(platform="tiktok", variant_path=str(tiny_video), title="דיגל", description="")
    real = ctx.db.add_post(vid, plan, status="planned")
    ctx.db.update_post(real, status="published", result=PostResult(
        platform="tiktok", ok=True, external_id="74", url="https://www.tiktok.com/@i/video/74"))
    fake_post = ctx.db.add_post(vid, plan, status="planned")
    ctx.db.update_post(fake_post, status="published", result=PostResult(
        platform="tiktok", ok=True, dry_run=True, external_id="dryrun-tiktok-1"))
    assert notifier.send_published(fake_post) is False
    assert notifier.send_published(real) is True
    msg = fake.sent()[-1]
    assert "עלה לטיקטוק" in msg["text"]
    assert msg["reply_markup"]["inline_keyboard"][0][0]["url"] == \
        "https://www.tiktok.com/@i/video/74"
    assert notifier.send_published(real) is False  # announced once


def test_disabled_or_without_token(ctx, monkeypatch):
    monkeypatch.setenv("AGENCY_KEYRING", "off")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    notifier = TelegramNotifier(ctx, transport=FakeBot())
    assert notifier.status()["state"] == "off" and notifier.start() is False
    ctx.settings.raw["notify"] = {"telegram": {"enabled": True}}
    assert notifier.status()["state"] == "not_configured" and notifier.start() is False
    assert tg.start_telegram(ctx) is None
    assert tg.notify_published(ctx, 1) is False
    tg.stop_telegram()


def test_urllib3_request_lines_are_redacted(caplog):
    caplog.set_level(logging.DEBUG)
    logging.getLogger("urllib3.connectionpool").debug(
        '%s://%s:%s "%s %s %s" %s', "https", "api.telegram.org", 443, "POST",
        f"/bot{TOKEN}/getUpdates", "HTTP/1.1", 200)
    assert TOKEN not in caplog.text and "/bot***/getUpdates" in caplog.text
