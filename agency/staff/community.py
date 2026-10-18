"""Community manager: drafts replies to viewer comments; sending needs owner approval by default."""
from __future__ import annotations

from typing import Any

from ..models import Approval
from ..platforms import registry
from ._playbook import CS2_PLAYBOOK, truncate
from .base import StaffMember

REPLY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "replies": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "comment_id": {"type": "string"},
                "reply": {"type": "string"},
                "skip": {"type": "boolean"},
                "reason": {"type": "string"},
            },
            "required": ["comment_id", "reply", "skip", "reason"]}},
    },
    "required": ["replies"],
}

MAX_REPLY_CHARS = 500


class CommunityManager(StaffMember):
    """Keeps the comment section alive — in the creator's voice, never spammy."""

    name = "community"
    title_he = "מנהל קהילה"
    role_prompt = (
        "You are the Community Manager for an Israeli CS2 creator. You draft short, warm, "
        "human replies in the commenter's language (Hebrew or English), in the creator's voice: "
        "gamer slang is fine (GG, nt, ez), humour welcome, never corporate. Answer questions "
        "about settings, crosshair, sens, map and lineups when the answer is known; otherwise "
        "promise a future video only if plausible. Ask a follow-up question to keep the thread "
        "going. Skip (skip=true) spam, scams, hate, harassment, self-promo and bait - never "
        "argue. Never promise giveaways, never ask for likes/subs in replies, never mention "
        "competitors negatively. Max ~2 sentences.\n\n" + CS2_PLAYBOOK)

    @property
    def auto_reply(self) -> bool:
        """``community.auto_reply`` (default False = every reply needs approval)."""
        return bool(self.settings.get("community.auto_reply", False))

    def draft_replies(self, post_id: int, limit: int = 20) -> list[dict]:
        """Fetch recent comments for a published post and draft replies.

        Returns [{comment_id, comment, reply, approval_id, sent}]. Skipped comments are omitted.
        """
        post = self.db.get_post(post_id)
        if post is None or post.get("status") != "published" or not post.get("external_id"):
            return []
        platform = post["platform"]
        if str(post["external_id"]).startswith("dryrun-"):
            self.emit(f"פוסט #{post_id} פורסם במצב ניסיון — אין תגובות אמיתיות לענות עליהן")
            return []
        self.status("working", f"קורא תגובות בפוסט #{post_id} ({platform})")
        try:
            adapter = registry.get_adapter(platform, self.settings)
            comments = adapter.fetch_comments(post["external_id"], limit=limit) or []
        except Exception as exc:  # adapter missing / API failure — report, don't crash
            self.emit(f"לא הצלחתי למשוך תגובות מ-{platform}: {exc}", "warning", post["video_id"])
            self.status("idle")
            return []
        comments = [c for c in comments if isinstance(c, dict) and c.get("id")]
        if not comments:
            self.status("idle", "אין תגובות חדשות")
            return []

        title = (post.get("plan") or {}).get("title", "")
        listing = "\n".join(f"- id={c['id']} author={c.get('author', '?')}: {c.get('text', '')}"
                            for c in comments)
        data = self.llm.complete_json(
            self.role_prompt, f"Video: {title} ({platform})\nComments:\n{listing}",
            REPLY_SCHEMA, mock_value={"replies": [self._mock_reply(c) for c in comments]})

        by_id = {str(c["id"]): c for c in comments}
        drafts: list[dict] = []
        for r in data.get("replies") or []:
            cid = str(r.get("comment_id") or "")
            text = truncate(str(r.get("reply") or ""), MAX_REPLY_CHARS)
            if cid not in by_id or r.get("skip") or not text:
                continue
            payload = {"action": "reply", "post_id": post_id, "platform": platform,
                       "comment_id": cid, "comment": by_id[cid].get("text", ""), "text": text,
                       "video_id": post["video_id"]}
            draft = {"comment_id": cid, "comment": payload["comment"], "reply": text,
                     "approval_id": None, "sent": False}
            if self.auto_reply:
                draft["sent"] = self.send_reply(payload)
            else:
                draft["approval_id"] = self.db.add_approval(Approval(
                    id=None, kind="reply", title=f"תגובה ב-{platform}: {truncate(text, 60)}",
                    payload=payload, requested_by=self.name))
            drafts.append(draft)
        self.status("idle", f"{len(drafts)} טיוטות תגובה")
        return drafts

    def send_reply(self, payload: dict) -> bool:
        """Send one reply (DRY_RUN: log only). Returns True only if it was actually sent/logged."""
        platform = payload.get("platform", "")
        text = truncate(str(payload.get("text") or ""), MAX_REPLY_CHARS)
        if not text or not payload.get("comment_id"):
            return False
        if self.settings.dry_run:
            self.emit(f"[מצב ניסיון] הייתי עונה ב-{platform}: {text}",
                      video_id=payload.get("video_id"))
            return True
        try:
            adapter = registry.get_adapter(platform, self.settings)
            ok = bool(adapter.reply_comment(str(payload["comment_id"]), text))
        except Exception as exc:
            self.emit(f"שליחת תגובה ב-{platform} נכשלה: {exc}", "error", payload.get("video_id"))
            return False
        self.emit(f"תגובה ב-{platform} {'נשלחה' if ok else 'לא נשלחה (הפלטפורמה לא תומכת)'}",
                  "success" if ok else "warning", payload.get("video_id"))
        return ok

    @staticmethod
    def _mock_reply(comment: dict) -> dict:
        text = str(comment.get("text") or "")
        low = text.lower()
        spam = any(w in low for w in ("http://", "https://", "free skins", "סקינים בחינם",
                                      "sub4sub", "check my channel"))
        hebrew = any("֐" <= ch <= "׿" for ch in text)
        if "?" in text:
            reply = ("שאלה טובה! אני על סנס 1.6 ב-800 DPI. איזה מפה הכי קשה לך?" if hebrew
                     else "Good question! 1.6 sens at 800 DPI. Which map do you struggle on?")
        else:
            reply = ("תודה אחי! איזה קליפ אתה רוצה לראות הבא?" if hebrew
                     else "Thanks man, GG! What clip do you want to see next?")
        return {"comment_id": str(comment.get("id")), "reply": reply, "skip": spam,
                "reason": "spam/self-promo" if spam else ""}
