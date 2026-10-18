"""Command line for the owner: `agency <command>`. Hebrew help; plain output.

Windows console rule (SPEC_V2 §11.11): the Windows console renders Hebrew reversed, so on
Windows every message is printed in ENGLISH unless ``AGENCY_CLI_LANG=he``. Hebrew lives in
the browser dashboard. Help texts stay Hebrew. ``agency`` with no command starts the dashboard.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

import typer
from rich.console import Console
from rich.table import Table

from .config import PACKAGE_ROOT, ConfigError, Settings, has_config, load_settings, \
    validate_settings

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:  # UTF-8 output (data such as video titles may still be Hebrew)
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

app = typer.Typer(
    name="agency",
    help="סוכנות השיווק של ערוץ ה-CS2 שלך: צוות עובדי AI עם מנכ\"ל אחד מעליהם. "
         "בלי פקודה — נפתח לוח הבקרה.",
    no_args_is_help=False,
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
STATUS_EN = {"awaiting_approval": "awaiting approval"}
KIND_HE = {"publish": "פרסום", "spend": "הוצאה", "reply": "תגובה", "other": "אחר"}
PLATFORM_ALIASES = {"instagram": "instagram_reels", "ig": "instagram_reels",
                    "reels": "instagram_reels", "shorts": "youtube_shorts", "yt": "youtube",
                    "twitter": "x", "fb": "facebook", "tg": "telegram"}
PLATFORM_EN = {"youtube": "YouTube", "youtube_shorts": "YouTube Shorts", "tiktok": "TikTok",
               "instagram_reels": "Instagram", "facebook": "Facebook", "x": "X",
               "reddit": "Reddit", "discord": "Discord", "telegram": "Telegram"}

_state: dict[str, Any] = {"config": None, "ctx": None}


# --------------------------------------------------------------------------- language
def cli_lang() -> str:
    """``"he"`` or ``"en"``: ``AGENCY_CLI_LANG`` wins; otherwise English on Windows."""
    forced = os.environ.get("AGENCY_CLI_LANG", "").strip().lower()
    if forced in ("he", "en"):
        return forced
    return "en" if IS_WINDOWS else "he"


def t(he_text: str, en_text: str) -> str:
    """Pick the message for the console language."""
    return he_text if cli_lang() == "he" else en_text


# --------------------------------------------------------------------------- helpers
def he(value: Any) -> str:
    """Label for a status/kind value in the console language (falls back to the value)."""
    text = str(value or "")
    if cli_lang() != "he":
        return STATUS_EN.get(text, text)
    return STATUS_HE.get(text, KIND_HE.get(text, text))


def pname(key: str) -> str:
    """Platform display name in the console language."""
    if cli_lang() != "he":
        return PLATFORM_EN.get(key, key)
    from .connect import NAMES_HE
    return NAMES_HE.get(key, key)


def _config_label() -> str:
    return str(_state["config"]) if _state["config"] else "config.yaml"


def _settings() -> Settings:
    """Load + validate config; a broken config.yaml becomes a friendly error."""
    import yaml

    try:
        settings = load_settings(_state["config"])
        validate_settings(settings)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or type(exc).__name__
        if mark is not None:
            where_he = f" בשורה {mark.line + 1}, עמודה {mark.column + 1}"
            where_en = f" at line {mark.line + 1}, column {mark.column + 1}"
        else:
            where_he = where_en = ""
        _fail(t(f"יש שגיאת כתיבה בקובץ {_config_label()}{where_he} ({problem}). "
                "בדוק מרכאות שלא נסגרו, נקודתיים והזחות (רווחים, לא Tab).",
                f"{_config_label()} has a syntax error{where_en} ({problem}). Check unclosed "
                "quotes, colons and indentation (spaces, not Tab)."))
    except (ConfigError, ValueError, TypeError) as exc:
        _fail(t(f"ערך לא תקין בקובץ {_config_label()}: {exc}",
                f"{_config_label()} has an invalid value (open the dashboard settings to fix "
                "it)."))
    except OSError as exc:
        _fail(t(f"אי אפשר לקרוא את {_config_label()}: {exc}",
                f"Cannot read {_config_label()}: {type(exc).__name__}"))
    if not _state["config"] and not has_config(settings) and not _state.get("warned"):
        _state["warned"] = True
        console.print(t(f"[yellow]אזהרה: לא נמצא config.yaml בתיקייה {settings.root} — עובד עם "
                        "הגדרות הדוגמה ותיקיית נתונים חדשה כאן. עבור לתיקיית הסוכנות "
                        "(למשל cd C:\\cs2-agency) או הרץ agency init.[/yellow]",
                        f"[yellow]Warning: no config.yaml in {settings.root} — using the example "
                        "settings and a new data folder here. Go to the agency folder (e.g. "
                        "cd C:\\cs2-agency) or run: agency init[/yellow]"))
    return settings


def _ctx():
    """Build (once) the shared Context: settings + DB + LLM."""
    if _state["ctx"] is None:
        from .staff.base import build_context
        _state["ctx"] = build_context(_settings())
    return _state["ctx"]


def _fail(message: str, code: int = 1) -> None:
    console.print(f"[red]{t('שגיאה:', 'Error:')}[/red] {message}", markup=True)
    raise typer.Exit(code)


def _ok(message: str) -> None:
    console.print(f"[green]✓[/green] {message}")


def _ils(amount: float) -> str:
    return f"{float(amount or 0):,.2f} ₪" if cli_lang() == "he" \
        else f"ILS {float(amount or 0):,.2f}"


def _short_time(iso: str | None) -> str:
    """ISO timestamp → local 'YYYY-MM-DD HH:MM' for display."""
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone() \
            .strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return str(iso)


def _live_keys(settings: Settings) -> list[str]:
    from .platforms import registry

    out = []
    for key in registry.ALL_KEYS:
        try:
            if registry.get_adapter(key, settings).is_live():
                out.append(key)
        except Exception:
            continue
    return out


def _dry_run_banner(settings: Settings) -> None:
    live = _live_keys(settings)
    if not live:
        console.print(t("[bold yellow]מצב אימון (DRY-RUN) פעיל — שום דבר לא עולה באמת לרשתות."
                        "[/bold yellow]",
                        "[bold yellow]Training mode (DRY-RUN) is on — nothing is really "
                        "uploaded.[/bold yellow]"))
    else:
        names = ", ".join(pname(k) for k in live)
        console.print(t(f"[bold green]שידור חי:[/bold green] {names} (השאר במצב אימון)",
                        f"[bold green]LIVE:[/bold green] {names} (the rest in training mode)"))


def _print_budget(b: dict) -> None:
    table = Table(title=t(f"תקציב חודש {b.get('month', '')}", f"Budget {b.get('month', '')}"),
                  show_header=False)
    table.add_column("field", style="bold")
    table.add_column("value", justify="right")
    table.add_row(t("תקציב קידום חודשי", "Monthly promo budget"), _ils(b.get("budget", 0)))
    table.add_row(t("הוצאות החודש", "Spent this month"), _ils(b.get("spent", 0)))
    table.add_row(t("נותר בתקציב", "Remaining"), _ils(b.get("remaining", 0)))
    table.add_row(t("הכנסות החודש", "Income this month"), _ils(b.get("income", 0)))
    table.add_row(t("רווח נקי", "Net"), _ils(b.get("net", 0)))
    table.add_row(t("תקרת הוצאה בודדת", "Max single spend"), _ils(b.get("max_single", 0)))
    console.print(table)


def _print_execution(result: dict) -> None:
    """Human summary of pipeline.execute_approval output."""
    if result.get("ok"):
        _ok(t("בוצע.", "Done."))
    else:
        console.print(t(f"[yellow]לא בוצע:[/yellow] {result.get('error', 'סיבה לא ידועה')}",
                        "[yellow]Not done[/yellow] (details in the dashboard)."))
    for r in result.get("results") or []:
        mark = "✓" if r.get("ok") else "✗"
        extra = t(" (ניסיון)", " (training)") if r.get("dry_run") else ""
        where = r.get("url") or r.get("external_id") or (
            r.get("error") if cli_lang() == "he" else ("failed" if not r.get("ok") else "")) or ""
        console.print(f"  {mark} {r.get('platform')}{extra} {where}")
    if result.get("scheduled_post_ids"):
        console.print(t("  פוסטים שתוזמנו: ", "  Scheduled posts: ")
                      + ", ".join(map(str, result["scheduled_post_ids"])))
    if result.get("instructions"):
        console.print(t("[bold]הוראות לביצוע ידני:[/bold]",
                        "[bold]Manual steps (see the dashboard for the full text):[/bold]"))
        for i, step in enumerate(result["instructions"], 1):
            console.print(f"  {i}. {step}", markup=False)
    if result.get("cancelled_post_ids"):
        console.print(t("  פוסטים שבוטלו: ", "  Cancelled posts: ")
                      + ", ".join(map(str, result["cancelled_post_ids"])))


def _platform_key(value: str, *, allow_telegram: bool = False) -> str:
    from .platforms.registry import ALL_KEYS

    key = PLATFORM_ALIASES.get(value.strip().lower(), value.strip().lower())
    valid = [*ALL_KEYS, *(["telegram"] if allow_telegram else [])]
    if key not in valid:
        _fail(t(f"פלטפורמה לא מוכרת: {value}. אפשר: {', '.join(valid)}",
                f"Unknown platform: {value}. Choose one of: {', '.join(valid)}"))
    return key


_PROBLEMS_EN: dict[str, str] = {}


def _problem_text(problem: str | None) -> str:
    """Hebrew problem → English on a Windows console (generic when unknown)."""
    if not problem:
        return ""
    if cli_lang() == "he":
        return problem
    if not _PROBLEMS_EN:
        from . import connect, importer
        _PROBLEMS_EN.update({
            connect.BAD_KEY_HE: "the key is wrong or expired",
            connect.NO_NET_HE: "the service does not answer (no internet?) — try again soon",
            connect.RATE_HE: "too many checks — try again in a few minutes",
            connect.PARTIAL_HE: "some details are still missing",
            connect.UNKNOWN_HE: "the check failed — try again",
            importer.RELOGIN_HE: "needs a new sign-in",
        })
    for he_text, en_text in _PROBLEMS_EN.items():
        if problem.startswith(he_text):
            return en_text
    return "needs attention — open the dashboard (Connections) for details"


# --------------------------------------------------------------------------- root
class _EnglishConsoleLog(logging.Handler):
    """English console mode: activity events are Hebrew (they belong in the dashboard), so a
    line with Hebrew in it is printed as a short English pointer instead (a Windows console
    shows Hebrew reversed). Plain ASCII lines pass through unchanged."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = record.getMessage()
            if not text.isascii():
                actor = text[1:text.index("]")] if text.startswith("[") and "]" in text else ""
                actor = actor if actor.isascii() else ""
                text = (f"[{actor}] " if actor else "") + \
                    f"{record.levelname.lower()}: details are in the dashboard (activity log)"
            sys.stderr.write(text + "\n")
        except Exception:  # pragma: no cover - logging must never break a command
            pass


def _console_logging() -> None:
    """In English console mode, keep Hebrew event lines off the console (and so off the
    console-captured log file). Only WARNING and up are shown, like Python's default."""
    logger = logging.getLogger("agency")
    if cli_lang() != "en":
        return
    if not any(isinstance(h, _EnglishConsoleLog) for h in logger.handlers):
        # Having a handler here also keeps Python's last-resort stderr printer (which would
        # print the Hebrew text as is) out of the way.
        logger.addHandler(_EnglishConsoleLog(logging.WARNING))


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context,
         config: Optional[Path] = typer.Option(
             None, "--config", "-c", help="נתיב לקובץ config.yaml (ברירת מחדל: התיקייה הנוכחית).")
         ) -> None:
    """סוכנות CS2 — כל הפקודות. בלי פקודה נפתח לוח הבקרה. התחלה: agency init"""
    _state["config"] = config
    _state["ctx"] = None
    _console_logging()
    if ctx.invoked_subcommand is None:
        _run_dashboard(None, None, False, 60.0)


# --------------------------------------------------------------------------- setup
@app.command(help="הכנה ראשונית: יוצר config.yaml ו-.env מהדוגמאות ותיקיית data.")
def init(force: bool = typer.Option(False, "--force", help="לדרוס config.yaml קיים (.env לעולם לא נדרס).")
         ) -> None:
    if _state["config"]:
        root = Path(_state["config"]).resolve().parent
    else:
        root = Path(os.environ.get("AGENCY_ROOT") or Path.cwd()).resolve()
    for example, target, may_overwrite in (("config.example.yaml", "config.yaml", force),
                                           (".env.example", ".env", False)):
        dst = root / target
        src = next((p for p in (root / example, PACKAGE_ROOT / example) if p.exists()), None)
        if dst.exists() and not may_overwrite:
            console.print(t(f"• {target} כבר קיים — לא שיניתי.",
                            f"• {target} already exists — left as is."))
            continue
        if src is None:
            console.print(t(f"[yellow]• לא נמצא {example} להעתקה.[/yellow]",
                            f"[yellow]• {example} not found.[/yellow]"))
            continue
        shutil.copyfile(src, dst)
        _ok(t(f"נוצר {target}", f"Created {target}"))
    settings = load_settings(root / "config.yaml" if (root / "config.yaml").exists() else None)
    (settings.data_dir / "videos").mkdir(parents=True, exist_ok=True)
    from .db import DB
    DB(settings.db_path)
    _ok(t(f"תיקיית הנתונים מוכנה: {settings.data_dir}", f"Data folder ready: {settings.data_dir}"))
    console.print()
    console.print(t("[bold]השלבים הבאים:[/bold]", "[bold]Next steps:[/bold]"))
    console.print(t("  1. יש כבר סטודיו או חיבורים קיימים? agency import",
                    "  1. Existing studio or connections? Run: agency import"))
    console.print(t("  2. הפעל את לוח הבקרה: agency (ושם: חיבורים והגדרות)",
                    "  2. Start the dashboard: agency (then Connections and Settings)"))
    console.print(t("  3. או העלה סרטון מכאן: agency upload path/to/clip.mp4",
                    "  3. Or upload a clip from here: agency upload path/to/clip.mp4"))
    console.print(t("  מצב אימון פעיל כברירת מחדל — שום דבר לא עולה באמת עד שתדליק פלטפורמה.",
                    "  Training mode is on by default — nothing goes out until you switch a "
                    "platform live."))


@app.command(help="העלאת סרטון חדש: הצוות מנתח, עורך, כותב ומתכנן פרסום.")
def upload(path: Path = typer.Argument(..., help="נתיב לקובץ הווידאו."),
           title: str = typer.Option("", "--title", "-t", help="רמז לכותרת / על מה הקליפ.")) -> None:
    from .dashboard.app import sanitize_filename

    src = path.expanduser()
    if not src.is_file():
        _fail(t(f"הקובץ לא נמצא: {src}", f"File not found: {src}"))
    try:
        safe = sanitize_filename(src.name)
    except ValueError as exc:
        _fail(t(str(exc), f"Unsupported file name or type: {src.name}"))
    ctx = _ctx()
    dest_dir = ctx.settings.data_dir / "videos"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe}"
    with console.status(t("מעתיק את הסרטון...", "Copying the video...")):
        shutil.copy2(src, dest)
    _dry_run_banner(ctx.settings)
    from .staff.manager import Manager

    with console.status(t("הצוות עובד על הסרטון (ניתוח, עריכה, טקסטים, תכנון)...",
                          "The team is working on the video (analysis, edit, copy, plan)...")):
        result = Manager(ctx).handle_new_video(dest, title)
    vid = result.get("video_id")
    if not result.get("ok"):
        _fail(t(f"סרטון #{vid} נכשל בשלב {result.get('failed_step', '?')}: {result.get('error')}",
                f"Video #{vid} failed at step {result.get('failed_step', '?')} "
                "(details in the dashboard)."))
    _ok(t(f"סרטון #{vid} עובד בהצלחה.", f"Video #{vid} processed."))
    analysis = result.get("analysis") or {}
    summary = analysis.get("summary_he") if cli_lang() == "he" else analysis.get("summary_en")
    if summary:
        console.print(t(f"תקציר: {summary}", f"Summary: {summary}"), markup=False)
    table = Table(title=t("תוכנית הפרסום", "Publishing plan"))
    table.add_column(t("פלטפורמה", "Platform"))
    table.add_column(t("כותרת", "Title"))
    table.add_column(t("מתי", "When"))
    if cli_lang() == "he":
        table.add_column("למה")
    for plan in result.get("plans") or []:
        row = [plan.get("platform", ""), plan.get("title", ""),
               _short_time(plan.get("scheduled_at"))]
        if cli_lang() == "he":
            row.append(str((plan.get("extra") or {}).get("why", "")))
        table.add_row(*row)
    console.print(table)
    if result.get("approval_id"):
        console.print(t(f"התוכנית מחכה לאישורך: agency approve {result['approval_id']}"
                        f"   (או לדחייה: agency reject {result['approval_id']})",
                        f"The plan waits for you: agency approve {result['approval_id']}"
                        f"   (or: agency reject {result['approval_id']})"))
    else:
        console.print(t("מצב אוטומטי: הפוסטים תוזמנו ויעלו בזמן שנקבע.",
                        "Auto mode: the posts are scheduled and go out on time."))
    for aid in result.get("promotion_approval_ids") or []:
        console.print(t(f"הצעת קידום ממתינה: #{aid}", f"Promotion proposal waiting: #{aid}"))


# --------------------------------------------------------------------------- status
@app.command(help="מצב הצוות, הסרטונים, האישורים והתקציב.")
def status() -> None:
    from .dashboard.app import status_snapshot

    ctx = _ctx()
    snap = status_snapshot(ctx)
    _dry_run_banner(ctx.settings)
    if cli_lang() == "he":
        mode = "אישור ידני" if snap["mode"] != "auto" else "אוטומטי"
        console.print(f"מצב פרסום: {mode} | מוח: {_brain_label(ctx.llm)}")
        console.print(f"תיקיית הסוכנות: {ctx.settings.root} | נתונים: {ctx.settings.data_dir}",
                      markup=False)
    else:
        mode = "owner approves" if snap["mode"] != "auto" else "automatic"
        console.print(f"Publishing: {mode} | Brain: {_brain_label(ctx.llm)}")
        console.print(f"Agency folder: {ctx.settings.root} | Data: {ctx.settings.data_dir}",
                      markup=False)
    table = Table(title=t("הצוות", "The team"))
    if cli_lang() == "he":
        table.add_column("תפקיד")
    table.add_column(t("שם", "Name"))
    table.add_column(t("מצב", "State"))
    if cli_lang() == "he":
        table.add_column("פירוט")
    for member in snap["staff"]:
        if cli_lang() == "he":
            table.add_row(member["title_he"], member["name"], he(member["status"]),
                          member.get("detail") or "")
        else:
            table.add_row(member["name"], he(member["status"]))
    console.print(table)
    c = snap["counts"]
    console.print(t(f"סרטונים: {c['videos']} | פוסטים: {c['posts']} | "
                    f"אישורים ממתינים: {c['pending_approvals']}",
                    f"Videos: {c['videos']} | Posts: {c['posts']} | "
                    f"Waiting for you: {c['pending_approvals']}"))
    if c["videos_by_status"]:
        console.print(t("סרטונים לפי מצב: ", "Videos by state: ") + ", ".join(
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
        console.print(t("אין אישורים שמחכים לך." if not all_ else "אין אישורים בכלל.",
                        "Nothing is waiting for you." if not all_ else "No approvals at all."))
        return
    table = Table(title=t("אישורים", "Approvals"))
    table.add_column("#", justify="right")
    table.add_column(t("סוג", "Kind"))
    table.add_column(t("נושא", "Subject"))
    table.add_column(t("עלות", "Cost"), justify="right")
    table.add_column(t("מצב", "State"))
    table.add_column(t("מבקש", "Asked by"))
    for a in rows:
        table.add_row(str(a.id), he(a.kind), a.title, _ils(a.cost_ils), he(a.status),
                      a.requested_by)
    console.print(table)
    for a in rows:
        view = approval_view(ctx, a)
        for plan in view.get("plans") or []:
            console.print(f"  #{a.id} · {plan['platform']}: {plan['title']} "
                          f"({_short_time(plan.get('scheduled_at'))})", markup=False)
    if not all_:
        console.print(t("לאישור: agency approve <מספר>   לדחייה: agency reject <מספר>",
                        "Approve: agency approve <number>   Reject: agency reject <number>"))


def _print_stuck_approvals(ctx) -> None:
    """Approvals the owner approved whose execution never finished (crash / closed window)."""
    stuck = ctx.db.list_approvals("approved")
    if stuck:
        console.print(t("[yellow]אושרו אבל לא בוצעו (למשל כי התוכנה נסגרה באמצע):[/yellow]",
                        "[yellow]Approved but not finished (e.g. the app was closed):[/yellow]"))
        for a in stuck:
            console.print(t(f"  #{a.id} {a.title} — להמשך: agency approve {a.id}",
                            f"  #{a.id} — to continue: agency approve {a.id}"), markup=False)


@app.command(help="אישור בקשה (פרסום / הוצאה / תגובה) לפי מספר.")
def approve(approval_id: int = typer.Argument(..., help="מספר האישור."),
            note: str = typer.Option("", "--note", "-n", help="הערה שלך."),
            now: bool = typer.Option(False, "--now", help="לפרסם מיד במקום בזמן המתוכנן.")) -> None:
    from .events import emit
    from .pipeline import execute_approval

    ctx = _ctx()
    try:
        approval = ctx.db.decide_approval(approval_id, True, note)
    except KeyError:
        _fail(t(f"אישור #{approval_id} לא נמצא.", f"Approval #{approval_id} not found."))
    except ValueError as exc:
        current = ctx.db.get_approval(approval_id)
        if current is None or current.status != "approved":
            _fail(t(f"אי אפשר לאשר: {exc}",
                    f"Cannot approve #{approval_id}: it is already "
                    f"{he(current.status) if current else 'gone'}."))
        # Already approved by the owner but never executed (interrupted): resume it.
        approval = current
        emit(ctx.db, "owner", f"הבעלים המשיך ביצוע של #{approval_id}: {approval.title}")
        _ok(t(f"#{approval_id} כבר אושר ולא בוצע — ממשיך בביצוע: {approval.title}",
              f"#{approval_id} was approved but not finished — continuing."))
    else:
        emit(ctx.db, "owner", f"הבעלים אישר את #{approval_id}: {approval.title}", "success")
        _ok(t(f"אישרת את #{approval_id}: {approval.title}", f"Approved #{approval_id}."))
    _dry_run_banner(ctx.settings)
    with console.status(t("מבצע...", "Working...")):
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
        _fail(t(f"אישור #{approval_id} לא נמצא.", f"Approval #{approval_id} not found."))
    except ValueError as exc:
        _fail(t(f"אי אפשר לדחות: {exc}", f"Cannot reject #{approval_id}: already decided."))
    emit(ctx.db, "owner", f"הבעלים דחה את #{approval_id}: {approval.title}", "warning")
    cleanup = execute_approval(ctx, approval_id)  # rejected → only cancels its posts
    _ok(t(f"דחית את #{approval_id}: {approval.title}", f"Rejected #{approval_id}."))
    if cleanup.get("cancelled_post_ids"):
        console.print(t("  פוסטים שבוטלו: ", "  Cancelled posts: ")
                      + ", ".join(map(str, cleanup["cancelled_post_ids"])))


# --------------------------------------------------------------------------- money
def _cfo():
    from .staff.finance import CFO
    return CFO(_ctx())


def _check_amount(amount: float) -> float:
    if not (amount == amount) or amount <= 0 or amount > 10_000_000:  # NaN-safe
        _fail(t("הסכום חייב להיות מספר חיובי.", "The amount must be a positive number."))
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
    _ok(t(f"נרשמה הוצאה של {_ils(amount)} ({category}).",
          f"Recorded an expense of {_ils(amount)} ({category})."))
    b = cfo.budget_status()
    if b["budget"] > 0 and b["spent"] > b["budget"]:
        console.print(t("[yellow]שים לב: ההוצאות החודש עברו את התקציב.[/yellow]",
                        "[yellow]Note: this month's spending is over the budget.[/yellow]"))
    console.print(t(f"נותר בתקציב: {_ils(b['remaining'])}", f"Remaining: {_ils(b['remaining'])}"))


@app.command(help="רישום הכנסה. דוגמה: agency income 120 adsense \"תשלום חודשי\"")
def income(amount: float = typer.Argument(..., help="סכום בשקלים."),
           source: str = typer.Argument(..., help="מקור (adsense, tiktok, sponsorship...)."),
           desc: str = typer.Argument("", help="תיאור.")) -> None:
    amount = _check_amount(amount)
    cfo = _cfo()
    cfo.record_income(amount, source, desc)
    _ok(t(f"נרשמה הכנסה של {_ils(amount)} ({source}).",
          f"Recorded income of {_ils(amount)} ({source})."))
    console.print(t(f"רווח נקי החודש: {_ils(cfo.budget_status()['net'])}",
                    f"Net this month: {_ils(cfo.budget_status()['net'])}"))


@app.command(help="דוח כספי חודשי בעברית.")
def report(month: Optional[str] = typer.Option(None, "--month", "-m", help="חודש בפורמט YYYY-MM.")
           ) -> None:
    text = _cfo().monthly_report(month)
    if cli_lang() == "he":
        console.print(text)
    else:
        b = _cfo().budget_status(month) if month else _cfo().budget_status()
        console.print("Monthly report (the full Hebrew report is in the dashboard):")
        _print_budget(b)


# --------------------------------------------------------------------------- manager
@app.command(help="תדריך יומי מהמנכ\"ל.")
def brief() -> None:
    from .llm import LLMError
    from .staff.manager import Manager

    try:
        with console.status(t("המנכ\"ל מכין תדריך...", "The manager is preparing a brief...")):
            text = Manager(_ctx()).daily_brief()
    except LLMError:
        _fail(t("המנכ\"ל לא זמין כרגע.", "The manager is not available right now."))
    if cli_lang() != "he":
        console.print("The manager's brief is in Hebrew — open the dashboard to read it "
                      "(the console shows Hebrew reversed).")
        return
    console.print(text, markup=False)


@app.command(help="שיחה עם המנכ\"ל. כתוב 'יציאה' כדי לסיים.")
def chat() -> None:
    from .llm import LLMError
    from .staff.manager import Manager

    ctx = _ctx()
    manager = Manager(ctx)
    if cli_lang() != "he":
        console.print("Tip: on Windows the chat is best in the dashboard (Hebrew shows "
                      "correctly there). Set AGENCY_CLI_LANG=he to chat here anyway.")
    if getattr(ctx.llm, "mock", False):
        console.print(t("[dim]מצב הדגמה: אין מפתח Claude, התשובות קבועות.[/dim]",
                        "[dim]Demo mode: no Claude key, answers are canned.[/dim]"))
    console.print(t("שלום! אני המנכ\"ל. במה אפשר לעזור? (יציאה = exit)",
                    "Hi! I'm the manager. What do you need? (type exit to quit)"))
    while True:
        try:
            message = console.input(t("[bold cyan]אתה> [/bold cyan]",
                                      "[bold cyan]you> [/bold cyan]")).strip()
        except (EOFError, KeyboardInterrupt):
            console.print()
            break
        if not message:
            continue
        if message.lower() in ("exit", "quit", "יציאה", "ביי", "q"):
            break
        try:
            with console.status(t("חושב...", "Thinking...")):
                reply = manager.chat(message)
        except LLMError:
            console.print(t("[red]שגיאה:[/red] המנכ\"ל לא זמין כרגע.",
                            "[red]Error:[/red] the manager is not available right now."))
            continue
        console.print(t("[bold]מנכ\"ל>[/bold] ", "[bold]manager>[/bold] "), end="")
        console.print(reply, markup=False)
    console.print(t("להתראות!", "Bye!"))


@app.command(help="משיכת נתוני צפייה מהפלטפורמות ותובנות שבועיות.")
def analytics() -> None:
    from .llm import LLMError
    from .staff.analytics import Analyst

    try:
        with console.status(t("מושך נתונים...", "Fetching numbers...")):
            result = Analyst(_ctx()).refresh()
    except LLMError:
        _fail(t("האנליסט לא זמין כרגע.", "The analyst is not available right now."))
    console.print(t(f"עודכנו נתונים ל-{result.get('fetched', 0)} פוסטים "
                    f"(שגיאות: {result.get('errors', 0)}).",
                    f"Updated numbers for {result.get('fetched', 0)} posts "
                    f"(errors: {result.get('errors', 0)})."))
    for key, label_he, label_en in (("best_platform", "הפלטפורמה הטובה ביותר", "Best platform"),
                                    ("best_hour", "השעה הטובה ביותר", "Best hour"),
                                    ("best_clip_type", "סוג הקליפ המוצלח", "Best clip type")):
        if result.get(key) not in (None, ""):
            console.print(f"{t(label_he, label_en)}: {result[key]}")
    if cli_lang() == "he":
        if result.get("insight_he"):
            console.print(result["insight_he"], markup=False)
        for action in result.get("actions") or []:
            console.print(f"  • {action}", markup=False)
    elif result.get("insight_he"):
        console.print("The weekly insight is in the dashboard.")


@app.command(help="פרסום הפוסטים שהגיע זמנם (מה שהמתזמן עושה כל דקה).")
def tick() -> None:
    from .scheduler import tick as run_tick

    ctx = _ctx()
    _dry_run_banner(ctx.settings)
    results = run_tick(ctx)
    if not results:
        console.print(t("אין פוסטים שהגיע זמנם.", "No posts are due."))
        return
    for r in results:
        mark = "✓" if r.ok else "✗"
        extra = t(" (ניסיון)", " (training)") if r.dry_run else ""
        detail = r.url or r.external_id or (r.error if cli_lang() == "he" else "failed") or ""
        console.print(f"{mark} {r.platform}{extra} {detail}", markup=False)


@app.command(help="ניסיון חוזר לפוסט שנכשל (למשל אחרי שגיאת quota ביוטיוב), לפי מספר פוסט.")
def retry(post_id: int = typer.Argument(..., help="מספר הפוסט (מופיע ביומן ובלוח הבקרה).")
          ) -> None:
    from .staff.publisher import Publisher

    ctx = _ctx()
    post = ctx.db.get_post(post_id)
    if post is None:
        _fail(t(f"פוסט #{post_id} לא נמצא.", f"Post #{post_id} not found."))
    if post["status"] != "failed":
        _fail(t(f"אפשר לנסות שוב רק פוסט שנכשל. פוסט #{post_id} במצב: {he(post['status'])}.",
                f"Only failed posts can be retried. Post #{post_id} is {he(post['status'])}."))
    _dry_run_banner(ctx.settings)
    with console.status(t(f"מעלה שוב את פוסט #{post_id}...", f"Uploading post #{post_id} again...")):
        result = Publisher(ctx).retry(post_id)
    if result.ok and (ctx.db.get_post(post_id) or {}).get("status") == "scheduled":
        _ok(t(f"פוסט #{post_id} ב-{result.platform} מתוזמן אצל Zernio ויעלה בזמן שנקבע.",
              f"Post #{post_id} on {result.platform} is scheduled at Zernio for its time."))
    elif result.ok:
        _ok(t(f"פוסט #{post_id} ב-{result.platform} פורסם{' (ניסיון)' if result.dry_run else ''} "
              f"{result.url or ''}",
              f"Post #{post_id} on {result.platform} published"
              f"{' (training)' if result.dry_run else ''} {result.url or ''}"))
    else:
        _fail(t(f"פוסט #{post_id} נכשל שוב: {result.error}",
                f"Post #{post_id} failed again (details in the dashboard)."))


# --------------------------------------------------------------------------- connections
@app.command(help="אילו פלטפורמות מחוברות, איך הן מעלות, ומה במצב אימון.")
def platforms() -> None:
    from . import connect as conn

    settings = _settings()
    _dry_run_banner(settings)
    table = Table(title=t("פלטפורמות", "Platforms"))
    table.add_column(t("פלטפורמה", "Platform"))
    table.add_column(t("מפתח", "Key"))
    table.add_column(t("פעילה", "On"))
    table.add_column(t("חיבור", "Connection"))
    table.add_column(t("דרך", "Via"))
    table.add_column(t("מצב", "Mode"))
    for row in conn.state(settings, check=False):
        enabled = bool(settings.get(f"publishing.platforms.{row['key']}.enabled", False))
        if row["connected"]:
            keys = t("מחובר", "connected")
            if row["source"] == "studio":
                keys += t(" (דרך הסטודיו הקיים)", " (via the existing studio)")
        elif row["missing"]:
            keys = t("חסר: ", "missing: ") + ", ".join(row["missing"])
        else:
            keys = t("לא מחובר", "not connected")
        mode = t("חי", "LIVE") if row["live"] else t("אימון", "training")
        table.add_row(pname(row["key"]), row["key"], t("כן", "yes") if enabled else t("לא", "no"),
                      keys, row["via"] or "-", mode)
    console.print(table)
    console.print(t("חיבור: agency connect <פלטפורמה> · העלאה אמיתית: agency live <פלטפורמה> on",
                    "Connect: agency connect <platform> · Go live: agency live <platform> on"))
    from .llm import LLM
    console.print(t("המוח של הצוות: ", "Team brain: ") + _brain_label(LLM(settings)),
                  markup=False)


def _brain_label(llm) -> str:
    """One line: who thinks for the team (English on Windows)."""
    try:
        info = llm.brain_info()
    except Exception:
        return t("מצב דמו", "demo mode (canned text)")
    label = info["label_he"] if cli_lang() == "he" else info["label_en"]
    if info.get("backend") == "claude_cli" and info.get("resting"):
        label += t(" — נח עד מחר (נגמרה התקרה היומית)", " — resting until tomorrow (daily cap used)")
    return label


@app.command(help="מי חושב בשביל הצוות: Claude שמותקן אצלך, מפתח בתשלום או מצב דמו.")
def brain() -> None:
    from .llm import LLM

    settings = _settings()
    llm = LLM(settings)
    info = llm.brain_info()
    console.print(t("המוח של הצוות: ", "Team brain: ") + _brain_label(llm), markup=False)
    console.print(t(f"הגדרה: llm.backend = {info['configured']}",
                    f"Setting: llm.backend = {info['configured']}"), markup=False)
    if info["backend"] == "claude_cli":
        cli = llm.cli
        console.print(t(f"Claude Code {cli.info.version}: {cli.info.shown_path}",
                        f"Claude Code {cli.info.version}: {cli.info.shown_path}"), markup=False)
        console.print(t(f"קריאות היום: {info['calls_today']} מתוך {info['daily_cap']}",
                        f"Calls today: {info['calls_today']} of {info['daily_cap']}"),
                      markup=False)
    elif info["backend"] == "mock":
        console.print(t("אין Claude Code מחובר ואין מפתח — הצוות כותב טקסטים מוכנים מראש. "
                        "התקן את Claude Code והתחבר פעם אחת, ואז הפעל מחדש.",
                        "No Claude Code and no API key found: the team writes canned text. "
                        "Install Claude Code, log in once, then restart the agency."),
                      markup=False)


@app.command("import", help="ייבוא חיבורים קיימים: הסטודיו הקיים, קבצי גוגל ו-.env (שמות בלבד, "
                            "אף מפתח לא מוצג).")
def import_cmd(paths: Optional[List[Path]] = typer.Argument(
                   None, help="תיקיות או קבצים נוספים לסריקה (למשל C:\\CS2_STUDIO)."),
               yes: bool = typer.Option(False, "--yes", "-y", help="לייבא בלי לשאול."),
               no_history: bool = typer.Option(False, "--no-history",
                                               help="בלי היסטוריית הפרסומים של הסטודיו.")
               ) -> None:
    from . import importer

    settings = _settings()
    paths = [Path(p).expanduser() for p in (paths or [])]
    studio_path = next((p for p in paths if importer.find_studio_dir(p) == p), None)
    report = importer.studio_report(settings, studio_path)
    if report["found"]:
        _print_studio_report(report)
        if yes or typer.confirm(t("לחבר את הסוכנות לסטודיו הקיים?",
                                  "Link the agency to the existing studio?"), default=True):
            res = importer.import_studio(settings, report["dir"], history=not no_history)
            _ok(t("הסוכנות מחוברת לסטודיו הקיים.", "The agency is linked to the existing studio."))
            if res.get("linked"):
                console.print(t("  מחובר דרך הסטודיו: ", "  Connected via the studio: ")
                              + ", ".join(pname(k) for k in res["linked"]))
            if res.get("history"):
                h = res["history"]
                console.print(t(f"  היסטוריית פרסומים: {h['imported']} חדשים, {h['skipped']} כבר היו",
                                f"  Post history: {h['imported']} imported, "
                                f"{h['skipped']} already here"))
            for problem in res.get("problems") or []:
                console.print(t(f"  [yellow]•[/yellow] {problem}",
                                "  [yellow]•[/yellow] Some studio items could not be imported "
                                "(see the dashboard)."))
            steps = res.get("next_connect") or []
            if steps:
                console.print(t("[bold]השלב הבא — להדביק מפתח (פעם אחת):[/bold]",
                                "[bold]Next — paste a key (once):[/bold]"))
                for step in steps:
                    console.print(f"  agency connect {step['key']}   "
                                  f"({t(step['title_he'], step['title_en'])})", markup=False)
    else:
        console.print(t("לא נמצא סטודיו קיים (C:\\CS2_STUDIO).",
                        "No existing studio found (C:\\CS2_STUDIO)."))

    with console.status(t("סורק קבצים...", "Scanning files...")):
        found = [f for f in importer.scan(paths, settings) if f.kind != "studio"]
    if not found:
        console.print(t("לא נמצאו קבצי חיבור נוספים.", "No other connection files found."))
        return
    table = Table(title=t("נמצא", "Found"))
    table.add_column("#", justify="right")
    table.add_column(t("מה", "What"))
    table.add_column(t("איפה", "Where"))
    table.add_column(t("הערה", "Note"))
    for i, item in enumerate(found, 1):
        table.add_row(str(i), _found_label(item), item.path, _problem_text(item.problem))
    console.print(table)
    importable = [f for f in found if f.ok]
    if not importable:
        return
    if not yes and not typer.confirm(t(f"לייבא {len(importable)} פריטים?",
                                       f"Import {len(importable)} item(s)?"), default=True):
        console.print(t("לא יובא כלום.", "Nothing imported."))
        return
    with console.status(t("מייבא ובודק חיבורים...", "Importing and checking connections...")):
        results = importer.import_found(importable, settings, verify=True)
    for res in results:
        if res.get("problem"):
            console.print(f"  ✗ {res['path']}: {_problem_text(res['problem'])}", markup=False)
            continue
        what = ", ".join(res.get("imported") or []) or t("כלום (כבר קיים)", "nothing (already set)")
        console.print(f"  ✓ {res['path']}: {what}", markup=False)
        if res.get("skipped"):
            console.print(t("    דילגתי (כבר קיים): ", "    skipped (already set): ")
                          + ", ".join(res["skipped"]))
        for key, ver in (res.get("verify") or {}).items():
            if ver.get("connected"):
                console.print(f"    {pname(key)}: {t('מחובר', 'connected')} "
                              f"{ver.get('account_name') or ''}", markup=False)
            else:
                why = _problem_text(ver.get("problem")) or t("לא מחובר", "not connected")
                console.print(f"    {pname(key)}: {why}", markup=False)


def _found_label(item) -> str:
    if cli_lang() == "he":
        return item.label_he
    return {"google_client_secret": "Google sign-in file (YouTube)",
            "google_token": "YouTube connection",
            "token_pickle": "old pickled token (not opened)",
            "env_file": "keys: " + ", ".join(item.names),
            "studio": "existing studio"}.get(item.kind, item.kind)


def _print_studio_report(report: dict) -> None:
    console.print(t(f"[bold]נמצא הסטודיו הקיים:[/bold] {report['dir']}",
                    f"[bold]Existing studio found:[/bold] {report['dir']}"), markup=True)
    if report.get("message_he"):
        console.print("  " + t(report["message_he"], report.get("message_en") or ""),
                      markup=False)
    entries = report.get("keyring") or []
    console.print(t("  חיבורים שמורים במחשב: ", "  Saved connections on this PC: ")
                  + (", ".join(entries) if entries else t("אין", "none")))
    ids = report.get("meta_ids") or {}
    console.print(t("  מזהי פייסבוק/אינסטגרם: ", "  Facebook/Instagram ids: ")
                  + (t("נמצאו", "found") if ids else t("לא נמצאו", "not found")))
    brand = report.get("brand") or {}
    if brand:
        console.print(t("  כללי מותג: ", "  Brand rules: ") + ", ".join(sorted(brand)))
    times = report.get("posting_times") or {}
    if times:
        console.print(t("  שעות פרסום: ", "  Posting times: ")
                      + ", ".join(f"{k} {v}" for k, v in times.items()))
    console.print(t(f"  פרסומים קודמים: {report.get('history_count', 0)}",
                    f"  Earlier posts: {report.get('history_count', 0)}"))
    daemon = report.get("daemon") or {}
    if daemon.get("active"):
        console.print(t("[yellow]  הסטודיו הישן עדיין פעיל — הסוכנות החדשה תחליף אותו?[/yellow]",
                        "[yellow]  The old studio is still running — should the new agency "
                        "replace it?[/yellow]"))
        console.print(t("  אנחנו לא מכבים אותו בעצמנו. כדי לכבות (בחלון פקודה כמנהל):",
                        "  We never turn it off ourselves. To turn it off (admin command "
                        "prompt):"))
        for cmd in daemon.get("disable_commands") or []:
            console.print(f"    {cmd}", markup=False)
        console.print(t("  עד אז הסוכנות נשארת במצב אימון כדי שלא יעלה כפול.",
                        "  Until then keep the agency in training mode so nothing is posted "
                        "twice."))


@app.command(help="חיבור פלטפורמה: מקלידים את המפתחות (מוסתרים) ונבדק חיבור אמיתי.")
def connect(platform: str = typer.Argument(
                ..., help="youtube | youtube_shorts | tiktok | instagram | facebook | x | reddit "
                          "| discord | telegram"),
            reset: bool = typer.Option(False, "--reset", help="להקליד מחדש גם מפתחות קיימים.")
            ) -> None:
    from . import connect as conn
    from .settings_store import SettingsError

    settings = _settings()
    key = _platform_key(platform, allow_telegram=True)
    tile = conn.platform_state(key, settings, check=False)
    if key in ("youtube", "youtube_shorts") and tile["via"] == "direct":
        _connect_youtube_direct(settings)
        return
    names = tile["required"] if reset else tile["missing"]
    if tile["source"] == "studio" and not names:
        console.print(t("החיבור מגיע מהסטודיו הקיים — בודק...",
                        "This connection comes from the existing studio — checking..."))
    values: dict[str, str] = {}
    for name in names:
        value = typer.prompt(t(f"{name} (מוסתר, Enter לדילוג)", f"{name} (hidden, Enter to skip)"),
                             default="", show_default=False, hide_input=True)
        if value.strip():
            values[name] = value
    try:
        with console.status(t("שומר ובודק חיבור...", "Saving and checking...")):
            tile = conn.save_keys(key, values, settings) if values \
                else conn.platform_state(key, settings, check=True)
    except (conn.ConnectError, SettingsError) as exc:
        _fail(t(str(exc), "That value was refused (wrong field or format)."))
    _print_tile(tile)


def _connect_youtube_direct(settings: Settings) -> None:
    from . import connect as conn

    console.print(t("[yellow]שים לב: העלאה ישירה מפרויקט גוגל לא מאומת עולה כפרטית. "
                    "ברירת המחדל היא Zernio.[/yellow]",
                    "[yellow]Note: direct uploads from an unverified Google project end up "
                    "private. The default route is Zernio.[/yellow]"))
    client = settings.data_dir / "secrets" / conn.CLIENT_SECRET_FILE
    if not client.is_file():
        raw = typer.prompt(t("נתיב לקובץ client_secret.json מגוגל", "Path to Google's "
                             "client_secret.json"), default="", show_default=False)
        path = Path(raw.strip().strip('"')).expanduser()
        if not path.is_file():
            _fail(t("הקובץ לא נמצא.", "File not found."))
        try:
            conn.save_client_secret(settings, path.read_bytes())
        except conn.ConnectError as exc:
            _fail(t(str(exc), "That is not Google's client_secret.json file."))
    console.print(t("נפתח דפדפן להתחברות עם גוגל...", "A browser opens for the Google sign-in..."))
    st = conn.start_youtube_oauth(settings, background=False)
    if st.get("state") == "done":
        _ok(t(f"מחובר ליוטיוב: {st.get('channel') or ''}", f"Connected to YouTube: "
              f"{st.get('channel') or ''}"))
    else:
        _fail(t(st.get("problem") or "ההתחברות נכשלה", "The Google sign-in did not finish."))


def _print_tile(tile: dict) -> None:
    name = pname(tile["key"])
    if tile["connected"]:
        via = t(" (דרך הסטודיו הקיים)", " (via the existing studio)") \
            if tile.get("source") == "studio" else ""
        _ok(t(f"{name} מחובר{via}: {tile.get('account_name') or ''}",
              f"{name} connected{via}: {tile.get('account_name') or ''}"))
        mode = t("העלאה אמיתית", "LIVE") if tile.get("live") else t("מצב אימון", "training mode")
        console.print(t(f"  מצב: {mode}", f"  Mode: {mode}"))
    elif tile.get("missing"):
        console.print(t(f"{name}: חסר ", f"{name}: missing ") + ", ".join(tile["missing"]))
    else:
        why = _problem_text(tile.get("problem")) or t("לא מחובר", "not connected")
        console.print(f"{name}: {why}", markup=False)


@app.command(help="העלאה אמיתית לפלטפורמה: on = מעכשיו עולה באמת (עם אישור), off = מצב אימון.")
def live(platform: str = typer.Argument(..., help="שם הפלטפורמה (youtube, tiktok, instagram...)."),
         switch: str = typer.Argument(..., help="on | off"),
         yes: bool = typer.Option(False, "--yes", "-y", help="בלי שאלת אישור.")) -> None:
    from .settings_store import SettingsError, set_platform_live

    settings = _settings()
    key = _platform_key(platform)
    value = switch.strip().lower()
    if value not in ("on", "off"):
        _fail(t("כתוב on או off.", "Write on or off."))
    on = value == "on"
    if on and not yes:
        question = t(f"מעכשיו הקליפים יעלו באמת ל-{pname(key)}. בטוח?",
                     f"From now on clips will REALLY upload to {pname(key)}. Sure?")
        if not typer.confirm(question, default=False):
            console.print(t("לא שיניתי כלום.", "Nothing changed."))
            return
    try:
        set_platform_live(settings, key, on)
    except SettingsError as exc:
        _fail(t(str(exc), f"{pname(key)} is not connected yet — run: agency connect {key}"
                if on else "The setting could not be saved."))
    if on:
        _ok(t(f"{pname(key)}: העלאה אמיתית.", f"{pname(key)}: LIVE uploads."))
    else:
        _ok(t(f"{pname(key)}: מצב אימון.", f"{pname(key)}: training mode."))
        if not settings.dry_run:
            console.print(t("[yellow]שים לב: dry_run כבוי בהגדרות, ולכן כל הפלטפורמות המחוברות "
                            "עדיין חיות.[/yellow]",
                            "[yellow]Note: dry_run is false in the settings, so every connected "
                            "platform is still live.[/yellow]"))
    if key == "youtube" and settings.get("publishing.platforms.youtube_shorts.live") is None:
        console.print(t("  יוטיוב Shorts משתמש באותו מתג.", "  YouTube Shorts follows this switch."))


@app.command(help="הפעלת לוח הבקרה בדפדפן (כולל המתזמן). זה גם מה ש-agency בלי פקודה עושה.")
def dashboard(host: Optional[str] = typer.Option(None, "--host", help="כתובת (ברירת מחדל מ-config)."),
              port: Optional[int] = typer.Option(None, "--port", "-p", help="פורט (ברירת מחדל מ-config)."),
              no_browser: bool = typer.Option(False, "--no-browser", help="לא לפתוח דפדפן."),
              interval: float = typer.Option(60, "--interval", help="כל כמה שניות המתזמן בודק.")
              ) -> None:
    _run_dashboard(host, port, no_browser, interval)


def _run_dashboard(host: Optional[str], port: Optional[int], no_browser: bool,
                   interval: float) -> None:
    import uvicorn

    from .dashboard import auth
    from .dashboard.app import create_app, start_services, stop_services

    ctx = _ctx()
    configured_host = str(ctx.settings.get("dashboard.host", "127.0.0.1") or "127.0.0.1")
    remote = auth.remote_enabled(ctx.settings)
    if host is None and remote and configured_host in ("127.0.0.1", "localhost", "::1"):
        host = "0.0.0.0"  # phone access (SPEC_V2 §6): reachable on the home Wi-Fi, PIN login
    host = host or configured_host
    port = int(port or int(float(ctx.settings.get("dashboard.port", 8765))))
    dash = ctx.settings.raw.setdefault("dashboard", {})
    dash["host"], dash["port"] = host, port  # runtime only (never written to config.yaml)
    app_ = create_app(ctx)
    key = getattr(app_.state, "access_key", None)
    if key:
        console.print(t("[yellow]אזהרה: לוח הבקרה פתוח לרשת המקומית. הכניסה רק עם הקישור "
                        "הסודי שמודפס כאן (הוא משתנה בכל הפעלה) — אל תשתף אותו.[/yellow]",
                        "[yellow]Warning: the dashboard is open to your home network. Entry only "
                        "with the private link printed here (new every start) — do not share "
                        "it.[/yellow]"))
    elif remote and host not in ("127.0.0.1", "localhost", "::1"):
        phones = auth.phone_urls(port)
        console.print(t("גישה מהטלפון פעילה (רק ב-Wi-Fi של הבית). בטלפון: "
                        f"{phones[0] if phones else '-'} — הקוד מופיע בהגדרות בלוח הבקרה.",
                        "Phone access is on (home Wi-Fi only). On the phone open: "
                        f"{phones[0] if phones else '-'} — the PIN is shown in the dashboard "
                        "settings on this PC."))
    stop = threading.Event()  # one shared stop event: scheduler, inbox watcher, Telegram bot
    start_services(app_, stop, interval_s=interval)
    browse_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{browse_host}:{port}/" + (f"?key={key}" if key else "")
    _dry_run_banner(ctx.settings)
    console.print(t(f"לוח הבקרה עולה בכתובת: {url}  (Ctrl+C לעצירה)",
                    f"Dashboard: {url}  (Ctrl+C to stop)"))
    if not no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        # proxy_headers=False: the client address is the real TCP peer (PIN-free loopback
        # access must never be granted because of an X-Forwarded-For header).
        uvicorn.run(app_, host=host, port=port, log_level="warning", proxy_headers=False)
    finally:
        stop.set()
        stop_services(app_)


if __name__ == "__main__":  # pragma: no cover
    app()
