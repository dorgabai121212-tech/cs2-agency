"""Command line for the owner: `agency <command>`. Hebrew help and plain Hebrew output."""
from __future__ import annotations

import os
import shutil
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import PACKAGE_ROOT, ConfigError, Settings, has_config, load_settings, \
    validate_settings

if sys.platform == "win32":  # Hebrew in the Windows console
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

app = typer.Typer(
    name="agency",
    help="סוכנות השיווק של ערוץ ה-CS2 שלך: צוות עובדי AI עם מנכ\"ל אחד מעליהם.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,  # never dump local variables (could hold secrets)
)
console = Console(highlight=False)

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".avi")

STATUS_HE = {
    "idle": "פנוי", "working": "עובד", "error": "שגיאה", "pending": "ממתין",
    "approved": "אושר", "executing": "בביצוע", "rejected": "נדחה", "executed": "בוצע",
    "new": "חדש",
    "analyzed": "נותח", "edited": "נערך", "planned": "מתוכנן",
    "awaiting_approval": "מחכה לאישור", "scheduled": "מתוזמן", "publishing": "בהעלאה",
    "published": "פורסם", "failed": "נכשל", "cancelled": "בוטל",
}
KIND_HE = {"publish": "פרסום", "spend": "הוצאה", "reply": "תגובה", "other": "אחר"}

_state: dict[str, Any] = {"config": None, "ctx": None}


# --------------------------------------------------------------------------- helpers
def he(value: Any) -> str:
    """Hebrew label for a status/kind value (falls back to the value itself)."""
    text = str(value or "")
    return STATUS_HE.get(text, KIND_HE.get(text, text))


def _config_label() -> str:
    return str(_state["config"]) if _state["config"] else "config.yaml"


def _settings() -> Settings:
    """Load + validate config; a broken config.yaml becomes a friendly Hebrew error."""
    import yaml

    try:
        settings = load_settings(_state["config"])
        validate_settings(settings)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" בשורה {mark.line + 1}, עמודה {mark.column + 1}" if mark is not None else ""
        problem = getattr(exc, "problem", None) or type(exc).__name__
        _fail(f"יש שגיאת כתיבה בקובץ {_config_label()}{where} ({problem}). "
              "בדוק מרכאות שלא נסגרו, נקודתיים והזחות (רווחים, לא Tab).")
    except (ConfigError, ValueError, TypeError) as exc:
        _fail(f"ערך לא תקין בקובץ {_config_label()}: {exc}")
    except OSError as exc:
        _fail(f"אי אפשר לקרוא את {_config_label()}: {exc}")
    if not _state["config"] and not has_config(settings) and not _state.get("warned"):
        _state["warned"] = True
        console.print(f"[yellow]אזהרה: לא נמצא config.yaml בתיקייה {settings.root} — עובד עם "
                      "הגדרות הדוגמה ותיקיית נתונים חדשה כאן. עבור לתיקיית הסוכנות "
                      "(למשל cd C:\\cs2-agency) או הרץ agency init.[/yellow]")
    return settings


def _ctx():
    """Build (once) the shared Context: settings + DB + LLM."""
    if _state["ctx"] is None:
        from .staff.base import build_context
        _state["ctx"] = build_context(_settings())
    return _state["ctx"]


def _fail(message: str, code: int = 1) -> None:
    console.print(f"[red]שגיאה:[/red] {message}", markup=True)
    raise typer.Exit(code)


def _ok(message: str) -> None:
    console.print(f"[green]✓[/green] {message}")


def _ils(amount: float) -> str:
    return f"{float(amount or 0):,.2f} ₪"


def _short_time(iso: str | None) -> str:
    """ISO timestamp → local 'YYYY-MM-DD HH:MM' for display."""
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone() \
            .strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(iso)


def _dry_run_banner(settings: Settings) -> None:
    if settings.dry_run:
        console.print("[bold yellow]מצב ניסיון (DRY-RUN) פעיל — שום דבר לא עולה באמת לרשתות."
                      "[/bold yellow]")


def _print_budget(b: dict) -> None:
    table = Table(title=f"תקציב חודש {b.get('month', '')}", show_header=False)
    table.add_column("שדה", style="bold")
    table.add_column("ערך", justify="right")
    table.add_row("תקציב קידום חודשי", _ils(b.get("budget", 0)))
    table.add_row("הוצאות החודש", _ils(b.get("spent", 0)))
    table.add_row("נותר בתקציב", _ils(b.get("remaining", 0)))
    table.add_row("הכנסות החודש", _ils(b.get("income", 0)))
    table.add_row("רווח נקי", _ils(b.get("net", 0)))
    table.add_row("תקרת הוצאה בודדת", _ils(b.get("max_single", 0)))
    console.print(table)


def _print_execution(result: dict) -> None:
    """Human summary of pipeline.execute_approval output."""
    if result.get("ok"):
        _ok("בוצע.")
    else:
        console.print(f"[yellow]לא בוצע:[/yellow] {result.get('error', 'סיבה לא ידועה')}")
    for r in result.get("results") or []:
        mark = "✓" if r.get("ok") else "✗"
        extra = " (ניסיון)" if r.get("dry_run") else ""
        where = r.get("url") or r.get("external_id") or r.get("error") or ""
        console.print(f"  {mark} {r.get('platform')}{extra} {where}")
    if result.get("scheduled_post_ids"):
        console.print(f"  פוסטים שתוזמנו: {', '.join(map(str, result['scheduled_post_ids']))}")
    if result.get("instructions"):
        console.print("[bold]הוראות לביצוע ידני:[/bold]")
        for i, step in enumerate(result["instructions"], 1):
            console.print(f"  {i}. {step}")
    if result.get("cancelled_post_ids"):
        console.print(f"  פוסטים שבוטלו: {', '.join(map(str, result['cancelled_post_ids']))}")


# --------------------------------------------------------------------------- root
@app.callback()
def main(config: Optional[Path] = typer.Option(
        None, "--config", "-c", help="נתיב לקובץ config.yaml (ברירת מחדל: התיקייה הנוכחית).")
         ) -> None:
    """סוכנות CS2 — כל הפקודות. התחל עם: agency init"""
    _state["config"] = config
    _state["ctx"] = None


# --------------------------------------------------------------------------- setup
@app.command(help="הכנה ראשונית: יוצר config.yaml ו-.env מהדוגמאות ותיקיית data.")
def init(force: bool = typer.Option(False, "--force", help="לדרוס config.yaml קיים (.env לעולם לא נדרס).")
         ) -> None:
    if _state["config"]:
        root = Path(_state["config"]).resolve().parent
    else:
        root = Path(os.environ.get("AGENCY_ROOT") or Path.cwd()).resolve()
    created = []
    for example, target, may_overwrite in (("config.example.yaml", "config.yaml", force),
                                           (".env.example", ".env", False)):
        dst = root / target
        src = next((p for p in (root / example, PACKAGE_ROOT / example) if p.exists()), None)
        if dst.exists() and not may_overwrite:
            console.print(f"• {target} כבר קיים — לא שיניתי.")
            continue
        if src is None:
            console.print(f"[yellow]• לא נמצא {example} להעתקה.[/yellow]")
            continue
        shutil.copyfile(src, dst)
        created.append(target)
        _ok(f"נוצר {target}")
    settings = load_settings(root / "config.yaml" if (root / "config.yaml").exists() else None)
    (settings.data_dir / "videos").mkdir(parents=True, exist_ok=True)
    from .db import DB
    DB(settings.db_path)
    _ok(f"תיקיית הנתונים מוכנה: {settings.data_dir}")
    console.print()
    console.print("[bold]השלבים הבאים:[/bold]")
    console.print("  1. פתח את הקובץ .env והדבק את ANTHROPIC_API_KEY (בלי מפתח — מצב הדגמה).")
    console.print("  2. ערוך את config.yaml: שם הערוץ, פלטפורמות פעילות ותקציב.")
    console.print("  3. הפעל את לוח הבקרה: agency dashboard")
    console.print("  4. או העלה סרטון מכאן: agency upload path/to/clip.mp4")
    console.print("  מצב ניסיון (dry_run) פעיל כברירת מחדל — שום דבר לא עולה באמת עד שתשנה.")


@app.command(help="העלאת סרטון חדש: הצוות מנתח, עורך, כותב ומתכנן פרסום.")
def upload(path: Path = typer.Argument(..., help="נתיב לקובץ הווידאו."),
           title: str = typer.Option("", "--title", "-t", help="רמז לכותרת / על מה הקליפ.")) -> None:
    from .dashboard.app import sanitize_filename

    src = path.expanduser()
    if not src.is_file():
        _fail(f"הקובץ לא נמצא: {src}")
    try:
        safe = sanitize_filename(src.name)
    except ValueError as exc:
        _fail(str(exc))
    ctx = _ctx()
    dest_dir = ctx.settings.data_dir / "videos"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe}"
    with console.status("מעתיק את הסרטון..."):
        shutil.copy2(src, dest)
    _dry_run_banner(ctx.settings)
    from .staff.manager import Manager

    with console.status("הצוות עובד על הסרטון (ניתוח, עריכה, טקסטים, תכנון)..."):
        result = Manager(ctx).handle_new_video(dest, title)
    vid = result.get("video_id")
    if not result.get("ok"):
        _fail(f"סרטון #{vid} נכשל בשלב {result.get('failed_step', '?')}: {result.get('error')}")
    _ok(f"סרטון #{vid} עובד בהצלחה.")
    analysis = result.get("analysis") or {}
    if analysis.get("summary_he"):
        console.print(f"תקציר: {analysis['summary_he']}")
    table = Table(title="תוכנית הפרסום")
    table.add_column("פלטפורמה")
    table.add_column("כותרת")
    table.add_column("מתי")
    table.add_column("למה")
    for plan in result.get("plans") or []:
        table.add_row(plan.get("platform", ""), plan.get("title", ""),
                      _short_time(plan.get("scheduled_at")),
                      str((plan.get("extra") or {}).get("why", "")))
    console.print(table)
    if result.get("approval_id"):
        console.print(f"התוכנית מחכה לאישורך: agency approve {result['approval_id']}"
                      f"   (או לדחייה: agency reject {result['approval_id']})")
    else:
        console.print("מצב אוטומטי: הפוסטים תוזמנו ויעלו בזמן שנקבע.")
    for aid in result.get("promotion_approval_ids") or []:
        console.print(f"הצעת קידום ממתינה: #{aid}")


# --------------------------------------------------------------------------- status
@app.command(help="מצב הצוות, הסרטונים, האישורים והתקציב.")
def status() -> None:
    from .dashboard.app import status_snapshot

    ctx = _ctx()
    snap = status_snapshot(ctx)
    _dry_run_banner(ctx.settings)
    mode = "אישור ידני" if snap["mode"] != "auto" else "אוטומטי"
    console.print(f"מצב פרסום: {mode} | מוח: {'הדגמה (mock)' if snap['llm_mock'] else 'Claude'}")
    console.print(f"תיקיית הסוכנות: {ctx.settings.root} | נתונים: {ctx.settings.data_dir}",
                  markup=False)
    table = Table(title="הצוות")
    table.add_column("תפקיד")
    table.add_column("שם")
    table.add_column("מצב")
    table.add_column("פירוט")
    for member in snap["staff"]:
        table.add_row(member["title_he"], member["name"], he(member["status"]),
                      member.get("detail") or "")
    console.print(table)
    c = snap["counts"]
    console.print(f"סרטונים: {c['videos']} | פוסטים: {c['posts']} | "
                  f"אישורים ממתינים: {c['pending_approvals']}")
    if c["videos_by_status"]:
        console.print("סרטונים לפי מצב: " + ", ".join(
            f"{he(k)} {v}" for k, v in c["videos_by_status"].items()))
    _print_budget(snap["budget"])


@app.command(help="רשימת האישורים שמחכים לך.")
def approvals(all_: bool = typer.Option(False, "--all", "-a", help="להציג גם אישורים שכבר הוחלטו.")
              ) -> None:
    from .dashboard.app import approval_view

    ctx = _ctx()
    rows = ctx.db.list_approvals(None if all_ else "pending")
    if not all_:
        _print_stuck_approvals(ctx)
    if not rows:
        console.print("אין אישורים שמחכים לך." if not all_ else "אין אישורים בכלל.")
        return
    table = Table(title="אישורים")
    table.add_column("#", justify="right")
    table.add_column("סוג")
    table.add_column("נושא")
    table.add_column("עלות", justify="right")
    table.add_column("מצב")
    table.add_column("מבקש")
    for a in rows:
        table.add_row(str(a.id), he(a.kind), a.title, _ils(a.cost_ils), he(a.status),
                      a.requested_by)
    console.print(table)
    for a in rows:
        view = approval_view(ctx, a)
        for plan in view.get("plans") or []:
            console.print(f"  #{a.id} · {plan['platform']}: {plan['title']} "
                          f"({_short_time(plan.get('scheduled_at'))})")
    if not all_:
        console.print("לאישור: agency approve <מספר>   לדחייה: agency reject <מספר>")


def _print_stuck_approvals(ctx) -> None:
    """Approvals the owner approved whose execution never finished (crash / closed window)."""
    stuck = ctx.db.list_approvals("approved")
    if stuck:
        console.print("[yellow]אושרו אבל לא בוצעו (למשל כי התוכנה נסגרה באמצע):[/yellow]")
        for a in stuck:
            console.print(f"  #{a.id} {a.title} — להמשך: agency approve {a.id}", markup=False)


@app.command(help="אישור בקשה (פרסום / הוצאה / תגובה) לפי מספר.")
def approve(approval_id: int = typer.Argument(..., help="מספר האישור."),
            note: str = typer.Option("", "--note", "-n", help="הערה שלך."),
            now: bool = typer.Option(False, "--now", help="לפרסם מיד במקום בזמן המתוכנן.")) -> None:
    from .pipeline import execute_approval

    ctx = _ctx()
    from .events import emit
    try:
        approval = ctx.db.decide_approval(approval_id, True, note)
    except KeyError:
        _fail(f"אישור #{approval_id} לא נמצא.")
    except ValueError as exc:
        current = ctx.db.get_approval(approval_id)
        if current is None or current.status != "approved":
            _fail(f"אי אפשר לאשר: {exc}")
        # Already approved by the owner but never executed (interrupted): resume it.
        approval = current
        emit(ctx.db, "owner", f"הבעלים המשיך ביצוע של #{approval_id}: {approval.title}")
        _ok(f"#{approval_id} כבר אושר ולא בוצע — ממשיך בביצוע: {approval.title}")
    else:
        emit(ctx.db, "owner", f"הבעלים אישר את #{approval_id}: {approval.title}", "success")
        _ok(f"אישרת את #{approval_id}: {approval.title}")
    _dry_run_banner(ctx.settings)
    with console.status("מבצע..."):
        result = execute_approval(ctx, approval_id, publish_now=True if now else None)
    _print_execution(result)


@app.command(help="דחיית בקשה לפי מספר.")
def reject(approval_id: int = typer.Argument(..., help="מספר האישור."),
           note: str = typer.Option("", "--note", "-n", help="למה דחית (עוזר לצוות ללמוד).")) -> None:
    from .events import emit
    from .pipeline import execute_approval

    ctx = _ctx()
    try:
        approval = ctx.db.decide_approval(approval_id, False, note)
    except KeyError:
        _fail(f"אישור #{approval_id} לא נמצא.")
    except ValueError as exc:
        _fail(f"אי אפשר לדחות: {exc}")
    emit(ctx.db, "owner", f"הבעלים דחה את #{approval_id}: {approval.title}", "warning")
    cleanup = execute_approval(ctx, approval_id)  # rejected → only cancels its posts
    _ok(f"דחית את #{approval_id}: {approval.title}")
    if cleanup.get("cancelled_post_ids"):
        console.print(f"  פוסטים שבוטלו: {', '.join(map(str, cleanup['cancelled_post_ids']))}")


# --------------------------------------------------------------------------- money
def _cfo():
    from .staff.finance import CFO
    return CFO(_ctx())


def _check_amount(amount: float) -> float:
    if not (amount == amount) or amount <= 0 or amount > 10_000_000:  # NaN-safe
        _fail("הסכום חייב להיות מספר חיובי.")
    return round(amount, 2)


@app.command(help="מצב התקציב החודשי: הוצאות, הכנסות ונותר.")
def budget() -> None:
    _print_budget(_cfo().budget_status())


@app.command(help="רישום הוצאה שכבר שילמת. דוגמה: agency expense 50 ads \"קידום בטיקטוק\"")
def expense(amount: float = typer.Argument(..., help="סכום בשקלים."),
            category: str = typer.Argument(..., help="קטגוריה (ads, software, gear...)."),
            desc: str = typer.Argument("", help="תיאור.")) -> None:
    amount = _check_amount(amount)
    cfo = _cfo()
    cfo.record_expense(amount, category, desc)
    _ok(f"נרשמה הוצאה של {_ils(amount)} ({category}).")
    b = cfo.budget_status()
    if b["budget"] > 0 and b["spent"] > b["budget"]:
        console.print("[yellow]שים לב: ההוצאות החודש עברו את התקציב.[/yellow]")
    console.print(f"נותר בתקציב: {_ils(b['remaining'])}")


@app.command(help="רישום הכנסה. דוגמה: agency income 120 adsense \"תשלום חודשי\"")
def income(amount: float = typer.Argument(..., help="סכום בשקלים."),
           source: str = typer.Argument(..., help="מקור (adsense, tiktok, sponsorship...)."),
           desc: str = typer.Argument("", help="תיאור.")) -> None:
    amount = _check_amount(amount)
    cfo = _cfo()
    cfo.record_income(amount, source, desc)
    _ok(f"נרשמה הכנסה של {_ils(amount)} ({source}).")
    console.print(f"רווח נקי החודש: {_ils(cfo.budget_status()['net'])}")


@app.command(help="דוח כספי חודשי בעברית.")
def report(month: Optional[str] = typer.Option(None, "--month", "-m", help="חודש בפורמט YYYY-MM.")
           ) -> None:
    console.print(_cfo().monthly_report(month))


# --------------------------------------------------------------------------- manager
@app.command(help="תדריך יומי מהמנכ\"ל.")
def brief() -> None:
    from .llm import LLMError
    from .staff.manager import Manager

    try:
        with console.status("המנכ\"ל מכין תדריך..."):
            text = Manager(_ctx()).daily_brief()
    except LLMError as exc:
        _fail(f"המנכ\"ל לא זמין כרגע: {exc}")
    console.print(text, markup=False)


@app.command(help="שיחה עם המנכ\"ל. כתוב 'יציאה' כדי לסיים.")
def chat() -> None:
    from .llm import LLMError
    from .staff.manager import Manager

    ctx = _ctx()
    manager = Manager(ctx)
    if getattr(ctx.llm, "mock", False):
        console.print("[dim]מצב הדגמה: אין מפתח Claude, התשובות קבועות.[/dim]")
    console.print("שלום! אני המנכ\"ל. במה אפשר לעזור? (יציאה = exit)")
    while True:
        try:
            message = console.input("[bold cyan]אתה> [/bold cyan]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not message:
            continue
        if message.lower() in ("exit", "quit", "יציאה", "ביי", "q"):
            break
        try:
            with console.status("חושב..."):
                reply = manager.chat(message)
        except LLMError as exc:
            console.print(f"[red]שגיאה:[/red] {exc}")
            continue
        console.print("[bold]מנכ\"ל>[/bold] ", end="")
        console.print(reply, markup=False)
    console.print("להתראות!")


@app.command(help="משיכת נתוני צפייה מהפלטפורמות ותובנות שבועיות.")
def analytics() -> None:
    from .llm import LLMError
    from .staff.analytics import Analyst

    try:
        with console.status("מושך נתונים..."):
            result = Analyst(_ctx()).refresh()
    except LLMError as exc:
        _fail(f"האנליסט לא זמין כרגע: {exc}")
    console.print(f"עודכנו נתונים ל-{result.get('fetched', 0)} פוסטים "
                  f"(שגיאות: {result.get('errors', 0)}).")
    for key, label in (("best_platform", "הפלטפורמה הטובה ביותר"),
                       ("best_hour", "השעה הטובה ביותר"), ("best_clip_type", "סוג הקליפ המוצלח")):
        if result.get(key) not in (None, ""):
            console.print(f"{label}: {result[key]}")
    if result.get("insight_he"):
        console.print(result["insight_he"], markup=False)
    for action in result.get("actions") or []:
        console.print(f"  • {action}", markup=False)


@app.command(help="פרסום הפוסטים שהגיע זמנם (מה שהמתזמן עושה כל דקה).")
def tick() -> None:
    from .scheduler import tick as run_tick

    ctx = _ctx()
    _dry_run_banner(ctx.settings)
    results = run_tick(ctx)
    if not results:
        console.print("אין פוסטים שהגיע זמנם.")
        return
    for r in results:
        mark = "✓" if r.ok else "✗"
        extra = " (ניסיון)" if r.dry_run else ""
        console.print(f"{mark} {r.platform}{extra} {r.url or r.external_id or r.error or ''}")


@app.command(help="ניסיון חוזר לפוסט שנכשל (למשל אחרי שגיאת quota ביוטיוב), לפי מספר פוסט.")
def retry(post_id: int = typer.Argument(..., help="מספר הפוסט (מופיע ביומן ובלוח הבקרה).")
          ) -> None:
    from .staff.publisher import Publisher

    ctx = _ctx()
    post = ctx.db.get_post(post_id)
    if post is None:
        _fail(f"פוסט #{post_id} לא נמצא.")
    if post["status"] != "failed":
        _fail(f"אפשר לנסות שוב רק פוסט שנכשל. פוסט #{post_id} במצב: {he(post['status'])}.")
    _dry_run_banner(ctx.settings)
    with console.status(f"מעלה שוב את פוסט #{post_id}..."):
        result = Publisher(ctx).retry(post_id)
    if result.ok:
        _ok(f"פוסט #{post_id} ב-{result.platform} פורסם{' (ניסיון)' if result.dry_run else ''} "
            f"{result.url or ''}")
    else:
        _fail(f"פוסט #{post_id} נכשל שוב: {result.error}")


@app.command(help="אילו פלטפורמות פעילות ומוגדרות (ומצב ניסיון).")
def platforms() -> None:
    from .dashboard.app import platforms_snapshot

    settings = _settings()
    _dry_run_banner(settings)
    table = Table(title="פלטפורמות")
    table.add_column("פלטפורמה")
    table.add_column("מפתח")
    table.add_column("פעילה")
    table.add_column("מפתחות API")
    table.add_column("מצב")
    for row in platforms_snapshot(settings):
        if row["error"]:
            keys = f"שגיאה: {row['error']}"
        elif row["configured"]:
            keys = "מוגדר"
        else:
            keys = "חסר: " + ", ".join(row["missing_secrets"]) if row["missing_secrets"] else "חסר"
        state = "חי" if row["live"] else ("ניסיון" if row["enabled"] else "-")
        table.add_row(row["display_he"], row["key"], "כן" if row["enabled"] else "לא", keys, state)
    console.print(table)
    console.print("מפתחות נשמרים רק בקובץ .env. מדריך: docs/PLATFORMS_SETUP.md")


@app.command(help="הפעלת לוח הבקרה בדפדפן (כולל המתזמן).")
def dashboard(host: Optional[str] = typer.Option(None, "--host", help="כתובת (ברירת מחדל מ-config)."),
              port: Optional[int] = typer.Option(None, "--port", "-p", help="פורט (ברירת מחדל מ-config)."),
              no_browser: bool = typer.Option(False, "--no-browser", help="לא לפתוח דפדפן."),
              interval: float = typer.Option(60, "--interval", help="כל כמה שניות המתזמן בודק.")
              ) -> None:
    import uvicorn

    from .dashboard.app import create_app
    from .scheduler import run_forever

    ctx = _ctx()
    host = host or str(ctx.settings.get("dashboard.host", "127.0.0.1"))
    port = int(port or int(float(ctx.settings.get("dashboard.port", 8765))))
    if host != str(ctx.settings.get("dashboard.host", "127.0.0.1")):
        ctx.settings.raw.setdefault("dashboard", {})["host"] = host  # app guards the real bind
    app_ = create_app(ctx)
    key = getattr(app_.state, "access_key", None)
    if key:
        console.print("[yellow]אזהרה: לוח הבקרה פתוח לרשת המקומית. הכניסה רק עם הקישור "
                      "הסודי שמודפס כאן (הוא משתנה בכל הפעלה) — אל תשתף אותו.[/yellow]")
    stop = threading.Event()
    threading.Thread(target=run_forever, args=(ctx,), kwargs={"interval_s": interval, "stop": stop},
                     daemon=True, name="scheduler").start()
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{browse_host}:{port}/" + (f"?key={key}" if key else "")
    _dry_run_banner(ctx.settings)
    console.print(f"לוח הבקרה עולה בכתובת: {url}  (Ctrl+C לעצירה)")
    if not no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        uvicorn.run(app_, host=host, port=port, log_level="warning")
    finally:
        stop.set()


if __name__ == "__main__":  # pragma: no cover
    app()
