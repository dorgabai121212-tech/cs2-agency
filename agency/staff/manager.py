"""The Manager (מנכ"ל): the ONE boss above the team. Runs the pipeline and talks to the owner.

The manager's tools are read-only or safe (they can create *pending* approvals or move a
not-yet-published post's time) — there is deliberately NO tool to approve, spend or publish.
"""
from __future__ import annotations

import json
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

MANAGER_PROMPT = """\
אתה המנכ"ל של סוכנות שיווק קטנה ופרטית שעובדת עבור יוצר תוכן ישראלי של Counter-Strike 2.
הבעלים (היוצר) מדבר איתך בעברית — ענה תמיד בעברית טבעית, קצרה וברורה, כמו בוס שמכיר את
הצוות שלו ואת עולם ה-CS2 (מפות, אייסים, קלאצ'ים, שורטס, טיקטוק, ריל, אלגוריתמים, שעות פרסום
בישראל ולקהל הבינלאומי).

הצוות שלך: אנליסט וידאו, עורך, קופירייטר, אסטרטג, מפרסם, מקדם, מנהל קהילה, אנליסט נתונים
ומנהל כספים (CFO). אתה מתאם ביניהם, מסביר החלטות ומציע צעדים הבאים.

חוקי ברזל (אין עליהם ויכוח):
1. כסף לא זז בלי אישור מפורש של הבעלים. אין לך שום כלי לאשר, להוציא כסף או לפרסם — רק הבעלים
   מאשר, בלוח הבקרה או בפקודה `agency approve`. גם אחרי אישור, ה-CFO חוסם כל הוצאה מעל התקציב
   החודשי או מעל תקרת הוצאה בודדת.
2. אין מעורבות מזויפת: לא בוטים, לא קניית צפיות/לייקים/עוקבים, לא ספאם, לא מניפולציה של הצבעות,
   לא sub4sub. מכבדים את תנאי השימוש ואת מגבלות הקצב של כל פלטפורמה. ברדיט ובדיסקורד מפרסמים רק
   במקומות שהבעלים הגדיר.
3. כל עוד dry_run פעיל — שום דבר לא עולה באמת; תגיד את זה בבירור.
4. כנות: לעולם אל תדווח על הצלחה שלא קרתה. אם משהו נכשל — תגיד מה ולמה.
5. סודות (מפתחות, סיסמאות) לעולם לא מופיעים בשיחה.

השתמש בכלים כדי לבדוק נתונים אמיתיים לפני שאתה עונה (סטטוס, אישורים ממתינים, תקציב, סרטונים,
פוסטים). אם הבעלים מבקש לאשר משהו — הסבר לו איך לאשר בעצמו (מספר האישור + הפקודה/הכפתור).
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
        self.status("working", f"סרטון חדש #{vid}: {Path(path).name}")
        result = run_pipeline(self.ctx, vid)
        if result.get("ok"):
            msg = (f"סרטון #{vid} מוכן — מחכה לאישור #{result['approval_id']}"
                   if result.get("approval_id") else f"סרטון #{vid} תוזמן לפרסום")
        else:
            msg = f"סרטון #{vid} נכשל: {result.get('error')}"
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
        self.status("working", "משוחח עם הבעלים")
        history = self.db.kv_get(_HISTORY_KEY, []) or []
        context = "\n".join(f"{h['role']}: {h['text']}" for h in history[-_HISTORY_TURNS:])
        user = (f"שיחה קודמת:\n{context}\n\nהודעה חדשה מהבעלים:\n{message}" if context
                else message)
        try:
            reply = self.llm.run_tools(self.role_prompt, user, self.tools(),
                                       mock_text=self._mock_chat(message))
        except LLMError as exc:
            reply = f"מצטער, לא הצלחתי להגיע ל-Claude כרגע ({exc}). נסה שוב עוד רגע."
        reply = reply or "לא קיבלתי תשובה מהמודל — נסה לנסח שוב."
        history = (history + [{"role": "owner", "text": message},
                              {"role": "manager", "text": reply}])[-2 * _HISTORY_TURNS:]
        self.db.kv_set(_HISTORY_KEY, history)
        self.status("idle")
        return reply

    def _mock_chat(self, message: str) -> str:
        """Deterministic offline answer built from real tool data."""
        s = self._t_status()
        b = CFO(self.ctx).budget_status()
        low = message.lower()
        lines = ["[מצב הדגמה — ללא Claude] "]
        if any(w in low for w in ("אשר", "approve", "תאשר")):
            lines.append("אני לא יכול לאשר בעצמי — רק אתה. ")
            pending = self.db.list_approvals("pending")
            if pending:
                lines.append(f"ממתין אישור #{pending[0].id}: {pending[0].title}. "
                             f"אשר עם `agency approve {pending[0].id}` או בלוח הבקרה.")
            else:
                lines.append("אין כרגע אישורים ממתינים.")
            return "".join(lines)
        lines.append(f"יש {s['videos_total']} סרטונים במערכת, {s['pending_approvals']} אישורים "
                     f"ממתינים ו-{s['scheduled_posts']} פוסטים מתוזמנים. ")
        lines.append(f"תקציב החודש: נוצלו {b['spent']:.0f} מתוך {b['budget']:.0f} ₪, הכנסות "
                     f"{b['income']:.0f} ₪. ")
        lines.append("מצב ניסיון פעיל — שום דבר לא עולה באמת." if s["dry_run"]
                     else "מצב אמיתי — פוסטים מאושרים עולים לרשתות.")
        return "".join(lines)

    # ------------------------------------------------------------------ brief
    def daily_brief(self) -> str:
        """Short Hebrew daily brief for the owner."""
        s = self._t_status()
        b = CFO(self.ctx).budget_status()
        tz = local_tz(self.settings)
        now = datetime.now(timezone.utc)
        due_soon = []
        for p in self.db.list_posts():
            when = parse_iso(p.get("scheduled_at"))
            if p["status"] == "scheduled" and when and now <= when <= now + timedelta(days=1):
                due_soon.append((when, p["platform"]))
        upcoming = [f"{plat} ב-{when.astimezone(tz):%H:%M}" for when, plat in sorted(due_soon)]
        failed = [v for v in self.db.list_videos(50) if v.status == VideoStatus.FAILED]
        last = self.db.kv_get("analytics.last") or {}
        facts = {"status": s, "budget": b, "upcoming_24h": upcoming,
                 "failed_videos": [v.id for v in failed],
                 "analytics": last.get("insight_he", "")}
        lines = [f"סיכום יומי מהמנכ״ל ({now.astimezone(tz):%d/%m/%Y}):",
                 f"- סרטונים: {s['videos_total']} | אישורים ממתינים: {s['pending_approvals']}"
                 + (f" (כולל {s['pending_spend_ils']:.0f} ₪ בקשות הוצאה)"
                    if s["pending_spend_ils"] else ""),
                 f"- מתוזמנים ל-24 השעות הקרובות: {', '.join(upcoming) if upcoming else 'אין'}",
                 f"- כספים: נוצלו {b['spent']:.0f}/{b['budget']:.0f} ₪, הכנסות {b['income']:.0f} ₪,"
                 f" נטו {b['net']:.0f} ₪"]
        if failed:
            lines.append(f"- דורש טיפול: סרטונים שנכשלו {', '.join(f'#{v.id}' for v in failed)}")
        if last.get("insight_he"):
            lines.append(f"- תובנה: {last['insight_he']}")
        lines.append("- מצב ניסיון פעיל: שום דבר לא עולה באמת." if s["dry_run"]
                     else "- מצב אמיתי פעיל.")
        try:
            data = self.llm.complete_json(
                self.role_prompt, "כתוב סיכום יומי קצר לבעלים על סמך הנתונים (בלי להמציא "
                f"מספרים):\n{json.dumps(facts, ensure_ascii=False, default=str)}",
                BRIEF_SCHEMA, mock_value={"brief_he": "\n".join(lines)})
            return str(data.get("brief_he") or "\n".join(lines))
        except LLMError:
            return "\n".join(lines)
