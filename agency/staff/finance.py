"""CFO: ledger, budget and hard spending caps. Money never moves without owner approval."""
from __future__ import annotations

from datetime import datetime, timezone

from ..models import Approval, LedgerEntry
from .base import StaffMember


def current_month() -> str:
    """"YYYY-MM" in UTC (ledger timestamps are UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m")


class CFO(StaffMember):
    """Keeps the books and enforces ``finance.monthly_budget_ils`` / ``max_single_spend_ils``."""

    name = "finance"
    title_he = "מנהל כספים (CFO)"
    role_prompt = (
        "You are the CFO of a small CS2 creator's agency. You are conservative: money is spent "
        "only with the owner's explicit approval and only within the monthly budget and the "
        "single-spend cap from config. You track income (AdSense, TikTok Creator Rewards, "
        "sponsorships, Twitch/Kick, affiliate codes) and expenses (ads, software, gear) in "
        "ILS, and you report honestly in Hebrew. You never recommend buying views, likes or "
        "followers.")

    # ------------------------------------------------------------------ caps
    @property
    def monthly_budget(self) -> float:
        return max(0.0, float(self.settings.get("finance.monthly_budget_ils", 0) or 0))

    @property
    def max_single(self) -> float:
        return max(0.0, float(self.settings.get("finance.max_single_spend_ils", 0) or 0))

    def budget_status(self, month: str | None = None) -> dict:
        """{budget, spent, remaining, income, net, max_single, month, currency}."""
        month = month or current_month()
        totals = self.db.month_totals(month)
        budget = self.monthly_budget
        return {"month": month, "currency": "ILS", "budget": budget,
                "spent": totals["expense"], "remaining": round(max(0.0, budget -
                                                                   totals["expense"]), 2),
                "income": totals["income"], "net": totals["net"],
                "max_single": self.max_single}

    def can_spend(self, amount: float, month: str | None = None) -> tuple[bool, str]:
        """Hard cap check (code, not prompt). Returns (allowed, Hebrew reason)."""
        status = self.budget_status(month)
        return self._check_caps(amount, status["spent"])

    def _check_caps(self, amount: float, spent: float) -> tuple[bool, str]:
        """Cap rules given what was already spent this month. Returns (allowed, reason)."""
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return False, "סכום לא תקין"
        if amount != amount or amount <= 0:  # NaN or non-positive
            return False, "סכום ההוצאה חייב להיות גדול מאפס"
        budget, max_single = self.monthly_budget, self.max_single
        remaining = round(max(0.0, budget - spent), 2)
        if budget <= 0:
            return False, "התקציב החודשי הוא 0 ₪ — אין הוצאות בכלל (finance.monthly_budget_ils)"
        if max_single <= 0:
            return False, "תקרת הוצאה בודדת היא 0 ₪ (finance.max_single_spend_ils)"
        if amount > max_single:
            return False, f"{amount:.2f} ₪ מעל תקרת הוצאה בודדת של {max_single:.2f} ₪"
        if spent + amount > budget + 1e-9:
            return False, (f"חריגה מהתקציב החודשי: נוצלו {spent:.2f} מתוך "
                           f"{budget:.2f} ₪, נשארו {remaining:.2f} ₪")
        return True, f"מאושר תקציבית: יישארו {remaining - amount:.2f} ₪ החודש"

    # ------------------------------------------------------------------ ledger
    def record_expense(self, amount: float, category: str, description: str = "", *,
                       platform: str | None = None, video_id: int | None = None) -> int:
        """Record an expense that ALREADY happened (entered by the owner or an executed approval)."""
        eid = self.db.add_ledger(LedgerEntry(id=None, kind="expense", amount_ils=float(amount),
                                             category=category, description=description,
                                             platform=platform, video_id=video_id))
        self.emit(f"נרשמה הוצאה: {float(amount):.2f} ₪ ({category}) {description}".strip(),
                  video_id=video_id)
        return eid

    def record_income(self, amount: float, source: str, description: str = "", *,
                      platform: str | None = None, video_id: int | None = None) -> int:
        """Record income (AdSense, TikTok, sponsorship...)."""
        eid = self.db.add_ledger(LedgerEntry(id=None, kind="income", amount_ils=float(amount),
                                             category=source, description=description,
                                             platform=platform, video_id=video_id))
        self.emit(f"נרשמה הכנסה: {float(amount):.2f} ₪ ({source}) {description}".strip(),
                  video_id=video_id)
        return eid

    def execute_spend(self, approval: Approval) -> dict:
        """Execute an APPROVED spend approval: re-check caps NOW, then record + instruct.

        We never call ad APIs: the owner launches the campaign by hand following the steps.
        """
        if approval.kind != "spend":
            return {"ok": False, "error": "not a spend approval"}
        if approval.status != "approved":
            return {"ok": False, "error": f"approval is {approval.status}, not approved"}
        payload = approval.payload or {}
        entry = LedgerEntry(id=None, kind="expense", amount_ils=float(approval.cost_ils),
                            category=payload.get("category", "ads"),
                            description=f"{approval.title} (אישור #{approval.id})",
                            platform=payload.get("platform"), video_id=payload.get("video_id"))
        # Cap check + ledger insert + "executed" in ONE write transaction: concurrent spends
        # (threads or a second process) are serialised, so the hard cap can never be exceeded
        # and a crash cannot leave money recorded without the approval marked executed.
        with self.db.immediate() as conn:
            if approval.id is not None:
                row = conn.execute("SELECT status FROM approvals WHERE id=?",
                                   (approval.id,)).fetchone()
                current = row["status"] if row else "missing"
                if current not in ("approved", "executing"):
                    return {"ok": False, "error": f"approval is {current}, not approved"}
            spent = self.db.month_expense_in(conn, current_month())
            allowed, reason = self._check_caps(approval.cost_ils, spent)
            entry_id = None
            if allowed:
                entry_id = self.db.insert_ledger_in(conn, entry)
                if approval.id is not None:
                    conn.execute("UPDATE approvals SET status='executed' WHERE id=? AND "
                                 "status IN ('approved','executing')", (approval.id,))
        if not allowed:
            self.emit(f"הוצאה #{approval.id} נדחתה למרות האישור: {reason}", "warning")
            return {"ok": False, "error": reason, "refused_by": "finance"}
        self.emit(f"נרשמה הוצאה: {entry.amount_ils:.2f} ₪ ({entry.category}) "
                  f"{entry.description}".strip(), video_id=entry.video_id)
        steps = payload.get("instructions") or [
            "היכנס לחשבון הפרסום של הפלטפורמה.",
            f"צור קמפיין בתקציב כולל של {approval.cost_ils:.2f} ₪ בדיוק.",
            "בחר את הסרטון שפורסם, קהל יעד: גיימרים / CS2.",
            "אשר והפעל. שמור את הקבלה."]
        return {"ok": True, "ledger_id": entry_id, "reason": reason, "instructions": steps}

    # ------------------------------------------------------------------ report
    def monthly_report(self, month: str | None = None) -> str:
        """Hebrew monthly report computed in code (numbers are never LLM-generated)."""
        status = self.budget_status(month)
        entries = self.db.list_ledger(status["month"])
        by_cat: dict[tuple[str, str], float] = {}
        for e in entries:
            key = (e.kind, e.category or "כללי")
            by_cat[key] = by_cat.get(key, 0.0) + e.amount_ils
        lines = [f"דוח כספי לחודש {status['month']}",
                 f"תקציב קידום: {status['budget']:.2f} ₪ | הוצאות: {status['spent']:.2f} ₪ | "
                 f"נותר: {status['remaining']:.2f} ₪",
                 f"הכנסות: {status['income']:.2f} ₪ | רווח נקי: {status['net']:.2f} ₪",
                 f"תקרת הוצאה בודדת: {status['max_single']:.2f} ₪"]
        incomes = [(c, v) for (k, c), v in by_cat.items() if k == "income"]
        expenses = [(c, v) for (k, c), v in by_cat.items() if k == "expense"]
        if incomes:
            lines.append("מקורות הכנסה: " + ", ".join(f"{c} {v:.2f} ₪" for c, v in incomes))
        if expenses:
            lines.append("הוצאות לפי קטגוריה: " + ", ".join(f"{c} {v:.2f} ₪"
                                                           for c, v in expenses))
        if not entries:
            lines.append("אין עדיין תנועות החודש. הכנסות מ-AdSense / TikTok / חסויות מזינים "
                         "ידנית: agency income סכום מקור תיאור")
        pending = [a for a in self.db.list_approvals("pending") if a.kind == "spend"]
        if pending:
            lines.append(f"ממתינות לאישורך {len(pending)} בקשות הוצאה בסך "
                         f"{sum(a.cost_ils for a in pending):.2f} ₪")
        if status["budget"] > 0 and status["spent"] > status["budget"] * 0.8:
            lines.append("שים לב: נוצל יותר מ-80% מהתקציב החודשי.")
        return "\n".join(lines)
