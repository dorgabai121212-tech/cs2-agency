"""The Manager (מנכ"ל): the ONE boss above the team. Runs the pipeline and talks to the owner.

The manager's tools are read-only or safe (they can create *pending* approvals or move a
not-yet-published post's time) — there is deliberately NO tool to approve, spend or publish.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..llm import LLMError, Tool
from ..models import PostPlan, VideoStatus
from ._playbook import local_tz, parse_iso, to_utc_iso
from .analytics import Analyst
from .base import StaffMember
from .finance import CFO
from .promoter import Promoter

BRIEF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"brief_he": {"type": "string"}},
    "required": ["brief_he"],
}

RESCHEDULABLE = {"planned", "awaiting_approval", "scheduled"}
_HISTORY_KEY = "manager.chat_history"
_HISTORY_TURNS = 6
# "שנה משהו" on a card pre-fills "לגבי <title>: "; offline we point to the card's editor.
_CHANGE_RE = re.compile(r"^\s*לגבי\s|תשנה|לשנות|שנה\s|תחליף|להחליף|כותרת|תיאור|האשטג")

MANAGER_PROMPT = """\
You are the CEO ("המנכ\"ל") of a small private AI marketing team that works for ONE creator: Dor,
who runs the Counter-Strike 2 channel "Imagination" (@imaginationcs2) - "Legendary CS2 moments.
Real demo. Shot like a movie." Your team: video analyst, editor, copywriter, strategist,
publisher, promoter, community manager, data analyst and CFO. You coordinate them, explain
their decisions and suggest the next step.

How you talk to Dor (strict):
- Hebrew only. Very short: at most 4 short sentences unless he asks for more. First person,
  warm, light gamer slang.
- Zero technical words. Never say API, token, JSON, config, dry-run, pipeline, webhook, OAuth,
  server, error codes or ids like "#17". Say the clip's title, platform names, "מצב אימון" for
  training mode and "חיבור" for a connection.
- At most ONE question per message, and make it a yes/no question
  (e.g. "להעלות את זה מחר ב-17:00?").
- End with one concrete next step when there is one.

Iron rules (no exceptions):
1. Money never moves without Dor's explicit approval (אישור). You have NO tool to approve,
   publish or spend - only Dor approves, on the home screen ("יאללה, תעלה") or in Telegram.
   Even after his approval the CFO refuses anything over the monthly budget or the single-spend
   cap. If he asks you to approve, tell him plainly that only he can, and where to tap.
2. No fake engagement: no bots (בוטים), no buying views/likes/followers (קניית צפיות), no spam,
   no vote manipulation, no sub4sub. Respect every platform's rules and limits. Reddit and
   Discord only in the places Dor listed.
3. Training mode (מצב אימון): while a platform is in training nothing goes up for real - say so
   plainly. Never say a clip "עלה" unless it really went live.
4. Honesty: never report a success that did not happen. If something failed, say what, in
   plain words, and offer to try again.
5. Secrets (keys, passwords) never appear in the conversation.
6. Never delete a video; at most suggest making one private - and only Dor decides.

Use the tools to check real data before you answer (status, waiting approvals, budget, videos,
posts). Tool results contain internal ids - never show them to Dor; use the clip's title.
"""


class Manager(StaffMember):
    """The ONE boss: orchestrates the pipeline, briefs and chats with the owner."""

    name = "manager"
    title_he = 'מנכ"ל'
    role_prompt = MANAGER_PROMPT

    # ------------------------------------------------------------------ pipeline
    def handle_new_video(self, path: str | Path, title_hint: str = "") -> dict:
        """Register a video and run the full pipeline (never raises for step failures)."""
        from ..pipeline import run_pipeline  # local import: pipeline imports staff modules

        vid = self.db.add_video(str(path), title_hint)
        label = (title_hint or Path(path).stem).strip()[:60]
        self.status("working", f"קליפ חדש: \"{label}\"")
        result = run_pipeline(self.ctx, vid)
        if result.get("ok"):
            msg = (f"\"{label}\" מוכן — מחכה לאישור שלך" if result.get("approval_id")
                   else f"\"{label}\" מתוזמן לפרסום")
        else:
            msg = f"משהו נתקע ב\"{label}\" — אפשר לנסות שוב"
        self.status("idle", msg)
        return result

    # ------------------------------------------------------------------ tools
    def tools(self) -> list[Tool]:
        """The manager's tool belt. Read-only/safe only — no approve, spend or publish tool."""
        obj = {"type": "object", "properties": {}}
        return [
            Tool("get_status", "Overall agency status: videos by status, pending approvals, "
                 "staff status, dry_run and publishing mode.", obj, self._t_status),
            Tool("list_approvals", "List approvals (default pending). Read-only.",
                 {"type": "object", "properties": {"status": {
                     "type": "string", "enum": ["pending", "approved", "rejected", "executed",
                                                "all"]}}}, self._t_approvals),
            Tool("budget_status", "This month's budget, spend, income and caps.", obj,
                 lambda _i: CFO(self.ctx).budget_status()),
            Tool("list_videos", "Recent videos with status and analysis summary.",
                 {"type": "object", "properties": {"limit": {"type": "integer"}}},
                 self._t_videos),
            Tool("list_posts", "Posts (optionally of one video) with platform, status, time.",
                 {"type": "object", "properties": {"video_id": {"type": "integer"}}},
                 self._t_posts),
            Tool("run_analytics", "Refresh stats from platforms and return insights.", obj,
                 lambda _i: Analyst(self.ctx).refresh()),
            Tool("propose_promotion", "Ask the promoter for promotion ideas for a video. Creates "
                 "PENDING approvals only; nothing is executed or paid.",
                 {"type": "object", "properties": {"video_id": {"type": "integer"}},
                  "required": ["video_id"]}, self._t_promote),
            Tool("schedule_reschedule", "Move a not-yet-published post to a new future time "
                 "(ISO 8601; naive = channel time zone). Does not publish or approve anything.",
                 {"type": "object", "properties": {"post_id": {"type": "integer"},
                                                   "iso": {"type": "string"}},
                  "required": ["post_id", "iso"]}, self._t_reschedule),
            Tool("explain_plan", "Explain the strategist's plan (platforms, times, why) for a "
                 "video.", {"type": "object", "properties": {"video_id": {"type": "integer"}},
                            "required": ["video_id"]}, self._t_explain),
        ]

    def _t_status(self, _input: dict | None = None) -> dict:
        videos = self.db.list_videos(500)
        by_status: dict[str, int] = {}
        for v in videos:
            by_status[v.status.value] = by_status.get(v.status.value, 0) + 1
        pending = self.db.list_approvals("pending")
        return {"dry_run": self.settings.dry_run,
                "publishing_mode": self.settings.get("publishing.mode", "approve"),
                "videos_total": len(videos), "videos_by_status": by_status,
                "pending_approvals": len(pending),
                "pending_spend_ils": round(sum(a.cost_ils for a in pending), 2),
                "scheduled_posts": sum(1 for p in self.db.list_posts()
                                       if p["status"] == "scheduled"),
                "staff": [{k: s[k] for k in ("name", "status", "detail")}
                          for s in self.db.list_staff_status()]}

    def _t_approvals(self, inp: dict) -> list[dict]:
        status = inp.get("status") or "pending"
        rows = self.db.list_approvals(None if status == "all" else status)
        return [{"id": a.id, "kind": a.kind, "title": a.title, "cost_ils": a.cost_ils,
                 "status": a.status, "requested_by": a.requested_by,
                 "created_at": a.created_at} for a in rows[:50]]

    def _t_videos(self, inp: dict) -> list[dict]:
        limit = max(1, min(50, int(inp.get("limit") or 10)))
        return [{"id": v.id, "file": Path(v.path).name, "title_hint": v.title_hint,
                 "status": v.status.value, "clip_type": v.analysis.get("clip_type"),
                 "map": v.analysis.get("map"), "score": v.analysis.get("score_1_10"),
                 "error": v.meta.get("error")} for v in self.db.list_videos(limit)]

    def _t_posts(self, inp: dict) -> list[dict]:
        vid = inp.get("video_id")
        posts = self.db.list_posts(int(vid) if vid is not None else None)
        return [{"id": p["id"], "video_id": p["video_id"], "platform": p["platform"],
                 "status": p["status"], "scheduled_at": p["scheduled_at"],
                 "title": (p.get("plan") or {}).get("title"), "url": p.get("url"),
                 "error": p.get("error"), "stats": p.get("stats")} for p in posts[:100]]

    def _t_promote(self, inp: dict) -> list[dict]:
        video = self.db.get_video(int(inp["video_id"]))
        if video is None:
            raise ValueError(f"video {inp['video_id']} not found")
        approvals = Promoter(self.ctx).propose(video, self.db.list_posts(video.id))
        return [{"approval_id": a.id, "kind": a.kind, "title": a.title, "cost_ils": a.cost_ils,
                 "status": "pending — owner must approve"} for a in approvals]

    def _t_reschedule(self, inp: dict) -> dict:
        post = self.db.get_post(int(inp["post_id"]))
        if post is None:
            raise ValueError(f"post {inp['post_id']} not found")
        if post["status"] not in RESCHEDULABLE:
            raise ValueError(f"post is {post['status']}; only unpublished posts can move")
        when = None
        try:
            when = datetime.fromisoformat(str(inp["iso"]).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"bad ISO time: {inp['iso']!r}") from exc
        if when.tzinfo is None:
            when = when.replace(tzinfo=local_tz(self.settings))
        if when <= datetime.now(timezone.utc) + timedelta(minutes=1):
            raise ValueError("new time must be in the future")
        plan = PostPlan.from_dict(post.get("plan") or {})
        plan.scheduled_at = to_utc_iso(when)
        self.db.update_post(post["id"], plan=plan)
        self.emit(f"פוסט #{post['id']} ({post['platform']}) הוזז ל-"
                  f"{when.astimezone(local_tz(self.settings)):%d/%m %H:%M}", video_id=post["video_id"])
        return {"post_id": post["id"], "scheduled_at": plan.scheduled_at,
                "status": post["status"]}

    def _t_explain(self, inp: dict) -> dict:
        vid = int(inp["video_id"])
        video = self.db.get_video(vid)
        if video is None:
            raise ValueError(f"video {vid} not found")
        tz = local_tz(self.settings)
        posts = []
        overall = ""
        for p in self.db.list_posts(vid):
            plan = p.get("plan") or {}
            extra = plan.get("extra") or {}
            overall = overall or extra.get("overall", "")
            when = parse_iso(p.get("scheduled_at"))
            posts.append({"post_id": p["id"], "platform": p["platform"], "status": p["status"],
                          "local_time": when.astimezone(tz).strftime("%a %d/%m %H:%M")
                          if when else None, "title": plan.get("title"),
                          "why": extra.get("why", "")})
        return {"video_id": vid, "status": video.status.value,
                "clip_type": video.analysis.get("clip_type"), "map": video.analysis.get("map"),
                "overall": overall, "posts": posts}

    # ------------------------------------------------------------------ chat
    def chat(self, message: str) -> str:
        """Owner ↔ manager conversation (Hebrew). Uses read-only/safe tools only."""
        self.status("working", "מדבר איתך", quiet=True)
        history = self.db.kv_get(_HISTORY_KEY, []) or []
        context = "\n".join(f"{h['role']}: {h['text']}" for h in history[-_HISTORY_TURNS:])
        user = (f"Earlier conversation:\n{context}\n\nNew message from Dor:\n{message}"
                if context else message)
        try:
            reply = self.llm.run_tools(self.role_prompt, user, self.tools(),
                                       mock_text=self._mock_chat(message))
        except LLMError:
            reply = "לא הצלחתי לחשוב על תשובה כרגע. ננסה שוב עוד רגע?"
        reply = reply or "לא קיבלתי תשובה. תנסח לי שוב?"
        history = (history + [{"role": "owner", "text": message},
                              {"role": "manager", "text": reply}])[-2 * _HISTORY_TURNS:]
        self.db.kv_set(_HISTORY_KEY, history)
        self.status("idle", quiet=True)
        return reply

    def _mock_chat(self, message: str) -> str:
        """Deterministic offline answer in the manager's voice, built from real state.

        Same style as the real one: Hebrew, at most 4 short sentences, no technical words,
        at most one yes/no question. Covers the dashboard's quick replies.
        """
        from ..home import home_sentence, results_summary

        low = message.lower()
        pending = self.db.list_approvals("pending")
        if any(w in low for w in ("אשר", "approve", "תאשר", "אישור")):
            if not pending:
                return "אני לא יכול לאשר בשבילך — רק אתה. כרגע אין שום דבר שמחכה לך."
            return ("אני לא יכול לאשר בשבילך — רק אתה. "
                    f"{self._pending_line(pending)} "
                    "לחץ \"יאללה, תעלה\" על הכרטיס במסך הבית.")
        if any(w in low for w in ("הוצא", "כסף", "תקציב", "budget", "spend")):
            b = CFO(self.ctx).budget_status()
            return (f"החודש הוצאנו {b['spent']:.0f} ₪ והכנסנו {b['income']:.0f} ₪. "
                    + (f"נשארו {b['remaining']:.0f} ₪ בתקציב." if b["budget"] else
                       "התקציב החודשי הוא 0 ₪, אז אני לא מוציא כלום."))
        if re.search(r"מה עלה|עלה היום|עלו היום|מה יצא", message) or "uploaded" in low:
            return self._uploaded_today()
        if any(w in low for w in ("רעיון", "idea")):
            return self._idea()
        if _CHANGE_RE.search(message):
            return self._change_hint(message, pending)
        line = home_sentence(self.ctx)
        week = results_summary(self.ctx, 7)
        videos = len(self.db.list_videos(500))
        parts = [line["sentence"]]
        if videos:
            parts.append(f"יש לנו {_count_videos(videos)} בסך הכל.")
        else:
            parts.append("עוד אין סרטונים אצלנו.")
        if week["views"]:
            parts.append(f"השבוע: {week['views']:,} צפיות.")
        if line["training"] and "אימון" not in line["sentence"]:
            parts.append("אנחנו במצב אימון, אז שום דבר לא יוצא החוצה בלי שתדליק.")
        return " ".join(parts[:4])

    def _change_hint(self, message: str, pending: list) -> str:
        """"שנה משהו" offline: point to the card's own editor (titles / descriptions)."""
        from ..home import video_title

        cards = []
        for a in pending:
            vid = (a.payload or {}).get("video_id")
            if a.kind == "publish" and vid is not None:
                cards.append(video_title(self.db.get_video(int(vid))))
        if not cards:
            return ("כרגע אין קליפ שמחכה לך. כשיהיה, אפשר לתקן כותרת ותיאור בכרטיס שלו, "
                    "ב\"פרטים לכל פלטפורמה\".")
        title = next((t for t in cards if t and t in message), cards[0])
        return (f"הכי מהיר שתתקן בעצמך: בכרטיס של \"{title}\" לחץ \"פרטים לכל פלטפורמה\", "
                "תקן ושמור. זה נשמר עוד לפני שאתה מאשר.")

    def _pending_line(self, pending: list) -> str:
        titles = [self._approval_label(a) for a in pending[:2]]
        if len(pending) == 1:
            return f"מחכה לך: {titles[0]}."
        return f"מחכים לך {len(pending)} דברים, למשל {titles[0]}."

    def _approval_label(self, a) -> str:
        from ..home import video_title

        vid = (a.payload or {}).get("video_id")
        if a.kind == "publish" and vid is not None:
            return f"\"{video_title(self.db.get_video(int(vid)))}\""
        if a.kind == "spend":
            return f"בקשת הוצאה של {a.cost_ils:.0f} ₪"
        return "בקשה קטנה"

    def _uploaded_today(self) -> str:
        from ..home import is_real_post, published_at, video_title

        tz = local_tz(self.settings)
        today = datetime.now(timezone.utc).astimezone(tz).date()
        real: dict[int, list[str]] = {}
        training: set[int] = set()
        for p in self.db.list_posts():
            when = published_at(p) if p["status"] == "published" else None
            if not when or when.astimezone(tz).date() != today:
                continue
            if is_real_post(p):
                real.setdefault(p["video_id"], []).append(_platform_he(p["platform"]))
            else:
                training.add(p["video_id"])
        if real:
            vid, platforms = next(iter(real.items()))
            more = f" ועוד {_count_videos(len(real) - 1)}" if len(real) > 1 else ""
            return (f"היום עלה \"{video_title(self.db.get_video(vid))}\" ל{', '.join(platforms)}"
                    f"{more}. רוצה שאבדוק איך הוא מתקדם?")
        if training:
            n = len(training)
            ready = "סרטון אחד מוכן" if n == 1 else f"{_count_videos(n)} מוכנים"
            return ("היום לא יצא כלום החוצה — הכל היה במצב אימון. "
                    f"{ready} באימון. רוצה להדליק העלאה אמיתית?")
        waiting = [a for a in self.db.list_approvals("pending") if a.kind == "publish"]
        if waiting:
            return f"היום עוד לא יצא כלום. {self._approval_label(waiting[0])} מחכה לאישור שלך."
        return "היום עוד לא יצא כלום. יש לך קליפ לזרוק?"

    def _idea(self) -> str:
        last = self.db.kv_get("analytics.last") or {}
        best = last.get("best_clip_type")
        clip = {"ace": "אייס", "clutch": "קלאץ'", "funny": "רגע מצחיק", "highlight": "הייליט",
                "tutorial": "טיפ", "montage": "מונטאז'"}.get(str(best or ""), "קלאץ' 1v3")
        why = "זה מה שעובד לך הכי טוב עכשיו." if best else "זה מה שהכי עובד ב-CS2 עכשיו."
        return (f"רעיון: {clip} עם דיגל, ושוט AI מטורף על הכדור האחרון. {why} "
                "שאכין לזה כותרות כשתזרוק את הקליפ?")

    # ------------------------------------------------------------------ brief
    def daily_brief(self) -> str:
        """Short Hebrew daily brief for the owner: a greeting line + at most 5 bullets."""
        from ..home import greeting, home_sentence, results_summary, video_title

        tz = local_tz(self.settings)
        now = datetime.now(timezone.utc)
        line = home_sentence(self.ctx, now)
        week = results_summary(self.ctx, 7, now)
        b = CFO(self.ctx).budget_status()
        bullets: list[str] = []
        if line["counts"]["problems"]:
            failed = [v for v in self.db.list_videos(50) if v.status == VideoStatus.FAILED]
            name = f" ב\"{video_title(failed[0])}\"" if failed else ""
            bullets.append(f"משהו נתקע{name} — אפשר לנסות שוב.")
        pending = self.db.list_approvals("pending")
        if pending:
            bullets.append(self._pending_line(pending))
        due_soon = []
        for p in self.db.list_posts():
            when = parse_iso(p.get("scheduled_at"))
            if p["status"] == "scheduled" and when and now <= when <= now + timedelta(days=1):
                due_soon.append((when, _platform_he(p["platform"])))
        if due_soon:
            when, name = min(due_soon)
            more = f" ועוד {len(due_soon) - 1}" if len(due_soon) > 1 else ""
            bullets.append(f"הבא בתור: {name} ב-{when.astimezone(tz):%H:%M}{more}.")
        if week["views"] or week["likes"]:
            bullets.append(f"השבוע: {week['views']:,} צפיות, {week['likes']:,} לייקים.")
        if line["training"]:
            bullets.append("מצב אימון: הכל נשאר אצלנו עד שתדליק העלאה אמיתית.")
        bullets.append(f"החודש: הוצאות {b['spent']:.0f} ₪ · הכנסות {b['income']:.0f} ₪.")
        last = self.db.kv_get("analytics.last") or {}
        if last.get("insight_he") and (last.get("posts_with_stats") or 0) > 0:
            bullets.append(_plain_platforms(str(last["insight_he"])))
        header = f"{greeting(self.settings, now)}! הנה מה שחשוב היום:"
        fallback = limit_bullets("\n".join([header, *[f"- {x}" for x in bullets]]))
        facts = {"home_line": line["text"], "bullets": bullets,
                 "week": {k: week[k] for k in ("views", "likes", "comments")}}
        try:
            data = self.llm.complete_json(
                self.role_prompt, "Write Dor's daily brief in Hebrew: one short greeting line, "
                "then at most 5 short bullets starting with '- '. Use ONLY these facts, no ids, "
                "no technical words, at most one yes/no question:\n"
                f"{json.dumps(facts, ensure_ascii=False, default=str)}",
                BRIEF_SCHEMA, mock_value={"brief_he": fallback})
            return limit_bullets(str(data.get("brief_he") or fallback))
        except LLMError:
            return fallback


_BULLET_RE = re.compile(r"^\s*(?:[-•*]|\d+[.)])\s+")
MAX_BRIEF_BULLETS = 5


def limit_bullets(text: str, max_bullets: int = MAX_BRIEF_BULLETS) -> str:
    """At most one header line plus ``max_bullets`` bullet lines (extra lines are dropped)."""
    header: list[str] = []
    bullets: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _BULLET_RE.match(line):
            if len(bullets) < max_bullets:
                bullets.append("- " + _BULLET_RE.sub("", line).strip())
        elif not header and not bullets:
            header.append(line)
        elif len(bullets) < max_bullets:  # a prose line counts as a bullet
            bullets.append(f"- {line}")
    return "\n".join(header + bullets)


def _platform_he(key: str) -> str:
    from ._playbook import PLATFORM_HE
    return PLATFORM_HE.get(key, key)


def _plain_platforms(text: str) -> str:
    """Replace internal platform keys ("youtube_shorts") with their Hebrew names."""
    from ._playbook import PLATFORM_HE
    for key in sorted(PLATFORM_HE, key=len, reverse=True):
        text = re.sub(rf"(?<![A-Za-z_]){re.escape(key)}(?![A-Za-z_])", PLATFORM_HE[key], text)
    return text


def _count_videos(n: int) -> str:
    if n == 1:
        return "סרטון אחד"
    words = {2: "שני", 3: "שלושה", 4: "ארבעה", 5: "חמישה", 6: "שישה", 7: "שבעה", 8: "שמונה",
             9: "תשעה", 10: "עשרה"}
    return f"{words.get(n, n)} סרטונים"
