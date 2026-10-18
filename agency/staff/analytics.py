"""Analytics: pulls stats for published posts and turns them into weekly insights."""
from __future__ import annotations

from typing import Any

from ..models import now_iso
from ..platforms import registry
from ._playbook import CS2_PLAYBOOK, local_tz, parse_iso
from .base import StaffMember

INSIGHT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"insight_he": {"type": "string"},
                   "actions": {"type": "array", "items": {"type": "string"}}},
    "required": ["insight_he", "actions"],
}

_METRICS = ("views", "likes", "comments", "shares")


def _num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class Analyst(StaffMember):
    """Numbers person: what worked, where, and when."""

    name = "analytics"
    title_he = "אנליסט נתונים"
    role_prompt = (
        "You are the Data Analyst of a CS2 creator's agency. From real per-post stats you find "
        "which platform, posting hour (channel time zone) and clip type perform best, using "
        "engagement rate ((likes+comments+shares)/views) and not just raw views, and you call "
        "out small sample sizes honestly. You write a short Hebrew weekly insight and 2-4 "
        "concrete next actions (e.g. 'more 1v3 clutches on Shorts at 19:00', 'hooks under 1s'). "
        "Never invent numbers that are not in the data.\n\n" + CS2_PLAYBOOK)

    def refresh(self) -> dict:
        """Fetch stats for real published posts, aggregate, store and return insights."""
        self.status("working", "מושך נתונים מהפלטפורמות")
        posts = [p for p in self.db.list_posts() if p["status"] == "published"]
        fetched = errors = 0
        adapters: dict[str, Any] = {}
        for p in posts:
            ext = p.get("external_id")
            if not ext or str(ext).startswith("dryrun-"):
                continue  # dry-run posts have no real stats
            try:
                adapter = adapters.get(p["platform"]) or registry.get_adapter(p["platform"],
                                                                              self.settings)
                adapters[p["platform"]] = adapter
                stats = adapter.fetch_stats(str(ext)) or {}
            except Exception as exc:
                errors += 1
                self.emit(f"שליפת נתונים לפוסט #{p['id']} נכשלה: {exc}", "warning", p["video_id"])
                continue
            if stats:
                stats = {**stats, "fetched_at": now_iso()}
                self.db.update_post(p["id"], stats=stats)
                p["stats"] = stats
                fetched += 1

        summary = self._aggregate(posts)
        mock = {"insight_he": self._mock_insight(summary), "actions": self._mock_actions(summary)}
        data = self.llm.complete_json(self.role_prompt, f"Aggregated stats: {summary}",
                                      INSIGHT_SCHEMA, mock_value=mock)
        result = {**summary, "fetched": fetched, "errors": errors,
                  "insight_he": str(data.get("insight_he") or mock["insight_he"]),
                  "actions": [str(a) for a in data.get("actions") or []],
                  "updated_at": now_iso()}
        self.db.kv_set("analytics.last", result)
        self.status("idle", f"עודכנו נתונים ל-{fetched} פוסטים")
        return result

    # ------------------------------------------------------------------ helpers
    def _aggregate(self, posts: list[dict]) -> dict:
        tz = local_tz(self.settings)
        clip_types: dict[int, str] = {}
        by: dict[str, dict[str, dict[str, float]]] = {"platform": {}, "hour": {}, "clip_type": {}}
        totals = {m: 0.0 for m in _METRICS}
        with_stats = 0
        for p in posts:
            stats = p.get("stats") or {}
            if not any(_num(stats.get(m)) for m in _METRICS):
                continue
            with_stats += 1
            vid = p["video_id"]
            if vid not in clip_types:
                video = self.db.get_video(vid)
                clip_types[vid] = ((video.analysis if video else {}) or {}).get("clip_type",
                                                                                "other")
            when = parse_iso(p.get("scheduled_at")) or parse_iso(p.get("updated_at"))
            keys = {"platform": p["platform"], "clip_type": clip_types[vid],
                    "hour": f"{when.astimezone(tz).hour:02d}:00" if when else "?"}
            for dim, key in keys.items():
                bucket = by[dim].setdefault(key, {**{m: 0.0 for m in _METRICS}, "posts": 0})
                bucket["posts"] += 1
                for m in _METRICS:
                    bucket[m] += _num(stats.get(m))
            for m in _METRICS:
                totals[m] += _num(stats.get(m))

        def best(dim: str) -> str | None:
            buckets = by[dim]
            if not buckets:
                return None
            return max(buckets, key=lambda k: (buckets[k]["views"] / max(1, buckets[k]["posts"]),
                                               buckets[k]["likes"]))

        views = totals["views"]
        engagement = ((totals["likes"] + totals["comments"] + totals["shares"]) / views
                      if views else 0.0)
        return {"posts_published": len(posts), "posts_with_stats": with_stats,
                "totals": totals, "engagement_rate": round(engagement, 4),
                "best_platform": best("platform"), "best_hour": best("hour"),
                "best_clip_type": best("clip_type"), "by_platform": by["platform"],
                "by_hour": by["hour"], "by_clip_type": by["clip_type"]}

    @staticmethod
    def _mock_insight(s: dict) -> str:
        if not s["posts_with_stats"]:
            return (f"פורסמו {s['posts_published']} פוסטים, אבל עדיין אין נתוני צפיות אמיתיים "
                    "(מצב ניסיון או שהפלטפורמות עוד לא מדווחות). נחזור לזה אחרי פרסום אמיתי.")
        t = s["totals"]
        return (f"השבוע: {int(t['views'])} צפיות, {int(t['likes'])} לייקים, אחוז מעורבות "
                f"{s['engagement_rate'] * 100:.1f}%. הפלטפורמה החזקה: {s['best_platform']}, "
                f"השעה הכי טובה: {s['best_hour']}, סוג הקליפ שעובד: {s['best_clip_type']}."
                + (" (מדגם קטן — להתייחס בזהירות)" if s["posts_with_stats"] < 5 else ""))

    @staticmethod
    def _mock_actions(s: dict) -> list[str]:
        if not s["posts_with_stats"]:
            return ["לפרסם לפחות 3 קליפים אמיתיים כדי שיהיה על מה למדוד",
                    "לשמור על הוק בשנייה הראשונה בכל שורט"]
        return [f"להעלות עוד קליפים מסוג {s['best_clip_type']} ל-{s['best_platform']}",
                f"לתזמן את הפוסט הבא בסביבות {s['best_hour']} (שעון ישראל)",
                "לבדוק את 2 השניות הראשונות של הקליפ החלש ביותר ולחזק את ההוק"]
