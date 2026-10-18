"""Promoter: organic promotion ideas + optional paid proposals (always as owner approvals)."""
from __future__ import annotations

from typing import Any

from ..brand import Brand, load_brand
from ..config import secret
from ..models import Approval, PostPlan, Video
from ._playbook import CS2_PLAYBOOK, truncate
from .base import StaffMember
from .finance import CFO

PROMO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pinned_comment": {"type": "string"},
        "community_post": {"type": "string"},
        "discord_message": {"type": "string"},
        "reddit_title": {"type": "string"},
        "paid": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "platform": {"type": "string", "enum": ["youtube", "tiktok", "meta"]},
                "amount_ils": {"type": "number"},
                "days": {"type": "integer"},
                "targeting": {"type": "string"},
                "why": {"type": "string"},
            },
            "required": ["platform", "amount_ils", "days", "targeting", "why"]}},
    },
    "required": ["pinned_comment", "community_post", "discord_message", "reddit_title", "paid"],
}

_AD_STEPS: dict[str, list[str]] = {
    "youtube": ["היכנס ל-ads.google.com (חשבון Google Ads המקושר לערוץ).",
                "קמפיין חדש ← וידאו ← 'הגדלת צפיות'. בחר את הסרטון מהערוץ.",
                "תקציב כולל: {amount} ₪ ל-{days} ימים. קהל: גיימרים, תחומי עניין Counter-Strike.",
                "מיקום: {targeting}. אשר ושמור את הקבלה."],
    "tiktok": ["פתח את TikTok ← הפוסט ← 'Promote' (או ads.tiktok.com).",
               "מטרה: צפיות בסרטון. תקציב כולל {amount} ₪ ל-{days} ימים.",
               "קהל: {targeting}. אשר ושמור את הקבלה."],
    "meta": ["היכנס ל-Meta Business Suite ← הרילס ← 'Boost'.",
             "תקציב כולל {amount} ₪ ל-{days} ימים, קהל: {targeting}.",
             "אשר ושמור את הקבלה."],
}


class Promoter(StaffMember):
    """Finds honest ways to get the clip seen. Never buys engagement; paid ads need approval."""

    name = "promoter"
    title_he = "מקדם"

    @property
    def brand(self) -> Brand:
        return load_brand(self.settings)

    @property
    def role_prompt(self) -> str:  # type: ignore[override]
        brand = self.brand
        return (
            f'You are the Promoter of "{brand.name}" ({brand.handle}), a Counter-Strike 2 '
            "channel of movie-grade moments rebuilt from real demos. You grow reach ONLY with "
            "legitimate methods: a pinned comment that sparks discussion, a YouTube community "
            "post, an announcement in the creator's OWN Discord, an honest Reddit post only in "
            "subreddits the owner listed (follow their rules, no reposting, no vote "
            "manipulation), and optionally small paid ads (YouTube/Google Ads, TikTok Promote, "
            "Meta boost) with a realistic ILS budget and international gamer targeting. You "
            "NEVER suggest buying views, likes, followers, bots, engagement pods, spam or "
            "sub4sub. Paid proposals must fit the remaining budget; if the budget is 0 propose "
            "no paid items.\n"
            "All public texts (pinned_comment, community_post, discord_message, reddit_title) "
            "are in ENGLISH, in the brand's voice: funny, trolling, confident, never "
            "self-mocking. The pinned comment asks ONE question that viewers want to answer. "
            "Paid targeting and 'why' are short plain Hebrew for the owner.\n\n"
            + brand.prompt_block() + "\n\n" + CS2_PLAYBOOK)

    def propose(self, video: Video, posts: list[dict]) -> list[Approval]:
        """Store organic texts in video.meta and create approvals for actions (crosspost/spend)."""
        label = (video.title_hint or "").strip()[:40]
        self.status("working", f"מכין רעיונות קידום ל\"{label}\"" if label
                    else "מכין רעיונות קידום")
        analysis = video.analysis or {}
        brand = self.brand
        cfo = CFO(self.ctx)
        budget = cfo.budget_status()
        main = next((p for p in posts if p.get("platform") in ("youtube", "youtube_shorts")),
                    posts[0] if posts else None)
        title = ((main or {}).get("plan") or {}).get("title") or video.title_hint or "CS2"
        data = self.llm.complete_json(
            self.role_prompt,
            (f"Video: {title}\nAnalysis: {analysis.get('clip_type')} on "
             f"{analysis.get('map')}, score {analysis.get('score_1_10')}/10, "
             f"{analysis.get('summary_en')}\nPlatforms planned: "
             f"{[p.get('platform') for p in posts]}\nRemaining paid budget this month: "
             f"{budget['remaining']} ILS (single spend cap {budget['max_single']} ILS)."),
            PROMO_SCHEMA, mock_value=self._mock(video, title, analysis, budget))

        for key in ("pinned_comment", "community_post", "reddit_title"):
            data[key] = brand.clean(data.get(key) or "")  # blocked words never go out
        data["discord_message"] = brand.enforce(data.get("discord_message") or title, "discord")
        self.db.update_video(video.id, meta={"promotion": {
            "pinned_comment": data.get("pinned_comment", ""),
            "community_post": data.get("community_post", "")}})

        approvals: list[Approval] = []
        variant = ((main or {}).get("plan") or {}).get("variant_path") or video.path
        approvals += self._crossposts(video, data, title, variant)
        approvals += self._paid(video, data, cfo)
        stored: list[Approval] = []
        for a in approvals:
            a.id = self.db.add_approval(a)
            stored.append(a)
        self.status("idle", f"{len(stored)} הצעות קידום ממתינות לאישור" if stored
                    else "טקסטי קידום מוכנים (ללא פעולות שדורשות אישור)")
        return stored

    # ------------------------------------------------------------------ helpers
    def _crossposts(self, video: Video, data: dict, title: str, variant: str) -> list[Approval]:
        out: list[Approval] = []
        platforms = self.settings.get("publishing.platforms", {}) or {}
        discord_on = bool(self.settings.get("promotion.discord_webhook_enabled")) or bool(
            (platforms.get("discord") or {}).get("enabled"))
        if discord_on and secret("DISCORD_WEBHOOK_URL"):
            plan = PostPlan(platform="discord", variant_path=variant, title=title,
                            description=truncate(data.get("discord_message") or title, 2000),
                            extra={"why": "הודעה בשרת הדיסקורד שלך", "promotion": True})
            out.append(Approval(id=None, kind="other", title=f"שליחת הסרטון לדיסקורד: {title}",
                                payload={"action": "crosspost", "video_id": video.id,
                                         "plan": plan.to_dict()},
                                requested_by=self.name))
        reddit = platforms.get("reddit") or {}
        subs = [s for s in reddit.get("subreddits") or [] if s]
        if reddit.get("enabled") and subs:
            for sub in subs:
                plan = PostPlan(platform="reddit", variant_path=variant,
                                title=truncate(data.get("reddit_title") or title, 300),
                                extra={"subreddit": sub, "subreddits": [sub],
                                       "why": f"פוסט אחד ב-r/{sub} לפי חוקי הקהילה",
                                       "promotion": True})
                out.append(Approval(id=None, kind="other", title=f"פוסט ב-r/{sub}: {plan.title}",
                                    payload={"action": "crosspost", "video_id": video.id,
                                             "plan": plan.to_dict()},
                                    requested_by=self.name))
        return out

    def _paid(self, video: Video, data: dict, cfo: CFO) -> list[Approval]:
        out: list[Approval] = []
        spent_in_proposals = 0.0
        for item in data.get("paid") or []:
            platform = str(item.get("platform") or "")
            try:
                amount = round(float(item.get("amount_ils") or 0), 2)
                days = max(1, int(item.get("days") or 3))
            except (TypeError, ValueError):
                continue
            ok, reason = cfo.can_spend(amount)
            if ok and not cfo.can_spend(amount + spent_in_proposals)[0]:
                ok, reason = False, "ההצעות יחד חורגות מהתקציב שנותר החודש"
            if platform not in _AD_STEPS or not ok:
                if platform in _AD_STEPS and amount > 0:
                    self.emit(f"הצעת קידום ממומן ({platform}, {amount} ₪) לא הוגשה: {reason}",
                              "warning", video.id)
                continue
            spent_in_proposals += amount
            targeting = str(item.get("targeting") or "קהל בינלאומי, גילאי 16-30, גיימינג")
            steps = [s.format(amount=f"{amount:.2f}", days=days, targeting=targeting)
                     for s in _AD_STEPS[platform]]
            out.append(Approval(
                id=None, kind="spend", cost_ils=amount, requested_by=self.name,
                title=f"קידום ממומן ב-{platform}: {amount:.2f} ₪ ל-{days} ימים",
                payload={"action": "paid_promo", "platform": platform, "video_id": video.id,
                         "days": days, "targeting": targeting, "category": "ads",
                         "why": item.get("why", ""), "instructions": steps}))
        return out

    def _mock(self, video: Video, title: str, analysis: dict, budget: dict) -> dict:
        cs_map = analysis.get("map") or "CS2"
        weapon = (analysis.get("weapons") or ["AK-47"])[0]
        paid: list[dict] = []
        amount = min(budget["remaining"], budget["max_single"], 50.0)
        if amount >= 10:
            paid.append({"platform": "youtube", "amount_ils": round(amount, 2), "days": 3,
                         "targeting": "קהל בינלאומי, גילאי 16-30, תחומי עניין: Counter-Strike",
                         "why": "הקליפ חזק — דחיפה קטנה בימים הראשונים עוזרת לאלגוריתם להמשיך "
                                "להפיץ אותו לבד."})
        reddit = f"{weapon} {analysis.get('clip_type') or 'moment'} on {cs_map}, rebuilt from " \
                 "the real demo (CS2)"
        return {
            "pinned_comment": f"Be honest: would you have taken that fight on {cs_map}? "
                              "Best answer gets pinned.",
            "community_post": f"New one is out: {title}. Which map should get the movie "
                              "treatment next?",
            "discord_message": f"New clip is out: {title}. Tell us what you think",
            "reddit_title": reddit.encode("ascii", "ignore").decode(),
            "paid": paid,
        }
