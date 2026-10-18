"""SQLite persistence. One file, a fresh connection per call (thread-safe), auto-migrating schema."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .models import Approval, LedgerEntry, PostPlan, PostResult, Video, VideoStatus, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL,
    title_hint TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL,
    analysis TEXT NOT NULL DEFAULT '{}',
    meta TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL,
    platform TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'planned',
    scheduled_at TEXT,
    result TEXT,
    external_id TEXT,
    url TEXT,
    error TEXT,
    stats TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    cost_ils REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    requested_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    amount_ils REAL NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    platform TEXT,
    video_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    message TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    video_id INTEGER
);
CREATE TABLE IF NOT EXISTS staff_status (
    name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_video ON posts(video_id);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status);
CREATE INDEX IF NOT EXISTS idx_ledger_created ON ledger(created_at);
"""

# Columns added after the first release go here: (table, column, DDL type/default).
MIGRATIONS: list[tuple[str, str, str]] = []

APPROVAL_TERMINAL = {"rejected", "executed"}


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return {} if default is None else default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return {} if default is None else default


def _iso_ago(seconds: float) -> str:
    """UTC ISO timestamp ``seconds`` ago, comparable with :func:`now_iso` strings."""
    return (datetime.now(timezone.utc) - timedelta(seconds=float(seconds))).isoformat(
        timespec="seconds")


def _check_ledger(e: LedgerEntry) -> None:
    if e.kind not in ("expense", "income"):
        raise ValueError(f"ledger kind must be expense|income, got {e.kind!r}")
    if float(e.amount_ils) < 0:
        raise ValueError("ledger amount must be >= 0")


class DB:
    """Thin data-access layer over a single SQLite file."""

    _init_lock = threading.Lock()

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self._init_lock, self._conn() as c:
            c.execute("PRAGMA journal_mode = WAL")
            c.executescript(SCHEMA)
            for table, column, ddl in MIGRATIONS:
                cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
                if column not in cols:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    # ------------------------------------------------------------------ videos
    @staticmethod
    def _row_to_video(r: sqlite3.Row) -> Video:
        try:
            status = VideoStatus(r["status"])
        except ValueError:
            status = VideoStatus.FAILED
        return Video(id=r["id"], path=r["path"], title_hint=r["title_hint"], status=status,
                     created_at=r["created_at"], analysis=_loads(r["analysis"]),
                     meta=_loads(r["meta"]))

    def add_video(self, path: str, title_hint: str = "") -> int:
        """Register a new video; returns its id."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO videos (path, title_hint, status, created_at) VALUES (?,?,?,?)",
                (str(path), title_hint or "", VideoStatus.NEW.value, now_iso()))
            return int(cur.lastrowid)

    def get_video(self, vid: int) -> Video | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
        return self._row_to_video(r) if r else None

    def list_videos(self, limit: int = 100) -> list[Video]:
        """Newest first."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM videos ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row_to_video(r) for r in rows]

    def update_video(self, vid: int, *, status: VideoStatus | None = None,
                     analysis: dict | None = None, meta: dict | None = None) -> None:
        """Update selected fields. ``meta`` is merged into existing meta; ``analysis`` replaces."""
        sets: list[str] = []
        args: list[Any] = []
        if status is not None:
            sets.append("status=?")
            args.append(VideoStatus(status).value)
        if analysis is not None:
            sets.append("analysis=?")
            args.append(_dumps(analysis))
        with self._conn() as c:
            if meta is not None:
                r = c.execute("SELECT meta FROM videos WHERE id=?", (vid,)).fetchone()
                merged = {**(_loads(r["meta"]) if r else {}), **meta}
                sets.append("meta=?")
                args.append(_dumps(merged))
            if sets:
                c.execute(f"UPDATE videos SET {', '.join(sets)} WHERE id=?", (*args, vid))

    # ------------------------------------------------------------------ posts
    @staticmethod
    def _row_to_post(r: sqlite3.Row) -> dict:
        d = dict(r)
        d["plan"] = _loads(d.get("plan"))
        d["result"] = _loads(d.get("result")) if d.get("result") else None
        d["stats"] = _loads(d.get("stats"))
        return d

    def add_post(self, video_id: int, plan: PostPlan, status: str = "planned") -> int:
        now = now_iso()
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO posts (video_id, platform, plan, status, scheduled_at, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?)",
                (video_id, plan.platform, _dumps(plan.to_dict()), status, plan.scheduled_at,
                 now, now))
            return int(cur.lastrowid)

    def get_post(self, post_id: int) -> dict | None:
        """Single post as dict (plan/result/stats decoded), or None."""
        with self._conn() as c:
            r = c.execute("SELECT * FROM posts WHERE id=?", (post_id,)).fetchone()
        return self._row_to_post(r) if r else None

    def update_post(self, post_id: int, *, status: str | None = None,
                    result: PostResult | None = None, stats: dict | None = None,
                    scheduled_at: str | None = None, plan: PostPlan | None = None) -> None:
        """Update a post. ``result`` also fills external_id/url/error columns."""
        sets = ["updated_at=?"]
        args: list[Any] = [now_iso()]
        if status is not None:
            sets.append("status=?")
            args.append(status)
        if result is not None:
            sets += ["result=?", "external_id=?", "url=?", "error=?"]
            args += [_dumps(result.to_dict()), result.external_id, result.url, result.error]
        if stats is not None:
            sets.append("stats=?")
            args.append(_dumps(stats))
        if plan is not None:
            sets += ["plan=?", "scheduled_at=?"]
            args += [_dumps(plan.to_dict()), plan.scheduled_at]
        if scheduled_at is not None:
            sets.append("scheduled_at=?")
            args.append(scheduled_at)
        with self._conn() as c:
            c.execute(f"UPDATE posts SET {', '.join(sets)} WHERE id=?", (*args, post_id))

    def list_posts(self, video_id: int | None = None) -> list[dict]:
        """Posts as dicts (newest first), optionally for one video."""
        with self._conn() as c:
            if video_id is None:
                rows = c.execute("SELECT * FROM posts ORDER BY id DESC").fetchall()
            else:
                rows = c.execute("SELECT * FROM posts WHERE video_id=? ORDER BY id",
                                 (video_id,)).fetchall()
        return [self._row_to_post(r) for r in rows]

    # ------------------------------------------------------------------ approvals
    @staticmethod
    def _row_to_approval(r: sqlite3.Row) -> Approval:
        return Approval(id=r["id"], kind=r["kind"], title=r["title"], payload=_loads(r["payload"]),
                        cost_ils=float(r["cost_ils"] or 0), status=r["status"],
                        requested_by=r["requested_by"], created_at=r["created_at"],
                        decided_at=r["decided_at"], note=r["note"] or "")

    def add_approval(self, a: Approval) -> int:
        """Always stored as ``pending`` — nothing can be created pre-approved."""
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO approvals (kind, title, payload, cost_ils, status, requested_by, "
                "created_at, note) VALUES (?,?,?,?,?,?,?,?)",
                (a.kind, a.title, _dumps(a.payload), float(a.cost_ils or 0), "pending",
                 a.requested_by or "", a.created_at or now_iso(), a.note or ""))
            return int(cur.lastrowid)

    def get_approval(self, aid: int) -> Approval | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        return self._row_to_approval(r) if r else None

    def list_approvals(self, status: str | None = "pending") -> list[Approval]:
        """Approvals with the given status (None = all), newest first."""
        with self._conn() as c:
            if status is None:
                rows = c.execute("SELECT * FROM approvals ORDER BY id DESC").fetchall()
            else:
                rows = c.execute("SELECT * FROM approvals WHERE status=? ORDER BY id DESC",
                                 (status,)).fetchall()
        return [self._row_to_approval(r) for r in rows]

    def decide_approval(self, aid: int, approved: bool, note: str = "") -> Approval:
        """Owner decision. Only a ``pending`` approval can be decided (raises otherwise).

        Atomic: a conditional UPDATE, so two concurrent decisions cannot both succeed.
        """
        with self._conn() as c:
            cur = c.execute(
                "UPDATE approvals SET status=?, decided_at=?, note=? "
                "WHERE id=? AND status='pending'",
                ("approved" if approved else "rejected", now_iso(), note or "", aid))
            r = c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
            if r is None:
                raise KeyError(f"approval {aid} not found")
            if cur.rowcount != 1:
                raise ValueError(f"approval {aid} is already {r['status']}")
        return self._row_to_approval(r)

    def _transition_approval(self, aid: int, from_statuses: tuple[str, ...], to_status: str
                             ) -> bool:
        """Atomically move an approval between statuses. True only for the winning caller."""
        marks = ",".join("?" * len(from_statuses))
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE approvals SET status=? WHERE id=? AND status IN ({marks})",
                (to_status, aid, *from_statuses))
            if cur.rowcount == 1:
                return True
            if c.execute("SELECT 1 FROM approvals WHERE id=?", (aid,)).fetchone() is None:
                raise KeyError(f"approval {aid} not found")
            return False

    def claim_approval(self, aid: int) -> bool:
        """Claim an ``approved`` approval for execution (-> ``executing``).

        Only one caller (thread or process) can win; the loser must not execute anything.
        """
        return self._transition_approval(aid, ("approved",), "executing")

    def release_approval(self, aid: int) -> bool:
        """Give back a claimed approval (``executing`` -> ``approved``), e.g. a refused spend."""
        return self._transition_approval(aid, ("executing",), "approved")

    def mark_approval_executed(self, aid: int) -> None:
        """Mark an approved (or claimed) approval as executed. Atomic; raises if it was not."""
        if not self._transition_approval(aid, ("approved", "executing"), "executed"):
            current = self.get_approval(aid)
            raise ValueError(f"approval {aid} is {current.status if current else '?'}, "
                             "not approved")

    def stale_approvals(self, status: str, older_than_s: float) -> list[Approval]:
        """Approvals in ``status`` whose decision is older than ``older_than_s`` seconds."""
        cutoff = _iso_ago(older_than_s)
        with self._conn() as c:
            rows = c.execute("SELECT * FROM approvals WHERE status=? AND "
                             "COALESCE(decided_at, created_at) <= ? ORDER BY id",
                             (status, cutoff)).fetchall()
        return [self._row_to_approval(r) for r in rows]

    # ------------------------------------------------------------------ post claims
    def claim_post(self, post_id: int, from_statuses: tuple[str, ...],
                   to_status: str = "publishing", *, scheduled_at: str | None = None) -> bool:
        """Atomically move a post from one of ``from_statuses`` to ``to_status``.

        Returns True only for the single caller that won (safe across threads and processes).
        """
        marks = ",".join("?" * len(from_statuses))
        sets, args = ["status=?", "updated_at=?"], [to_status, now_iso()]
        if scheduled_at is not None:
            sets.append("scheduled_at=?")
            args.append(scheduled_at)
        with self._conn() as c:
            cur = c.execute(
                f"UPDATE posts SET {', '.join(sets)} WHERE id=? AND status IN ({marks})",
                (*args, post_id, *from_statuses))
            return cur.rowcount == 1

    def stale_posts(self, status: str, older_than_s: float) -> list[dict]:
        """Posts in ``status`` not updated for ``older_than_s`` seconds."""
        cutoff = _iso_ago(older_than_s)
        with self._conn() as c:
            rows = c.execute("SELECT * FROM posts WHERE status=? AND updated_at <= ? ORDER BY id",
                             (status, cutoff)).fetchall()
        return [self._row_to_post(r) for r in rows]

    # ------------------------------------------------------------------ transactions
    @contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        """A connection holding ``BEGIN IMMEDIATE`` (one writer at a time, across processes).

        Use for check-then-write sequences that must not interleave (e.g. budget caps).
        Commits on success, rolls back on any exception.
        """
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    @staticmethod
    def month_expense_in(conn: sqlite3.Connection, month: str) -> float:
        """Sum of the month's expenses read through ``conn`` (inside its transaction)."""
        r = conn.execute("SELECT COALESCE(SUM(amount_ils), 0) AS s FROM ledger "
                         "WHERE kind='expense' AND created_at LIKE ?", (f"{month}%",)).fetchone()
        return round(float(r["s"] or 0), 2)

    @staticmethod
    def insert_ledger_in(conn: sqlite3.Connection, e: LedgerEntry) -> int:
        """Insert a ledger row through ``conn`` (inside its transaction)."""
        _check_ledger(e)
        cur = conn.execute(
            "INSERT INTO ledger (kind, amount_ils, category, description, platform, video_id, "
            "created_at) VALUES (?,?,?,?,?,?,?)",
            (e.kind, float(e.amount_ils), e.category or "", e.description or "", e.platform,
             e.video_id, e.created_at or now_iso()))
        return int(cur.lastrowid)

    # ------------------------------------------------------------------ ledger
    @staticmethod
    def _row_to_ledger(r: sqlite3.Row) -> LedgerEntry:
        return LedgerEntry(id=r["id"], kind=r["kind"], amount_ils=float(r["amount_ils"]),
                           category=r["category"], description=r["description"],
                           platform=r["platform"], video_id=r["video_id"],
                           created_at=r["created_at"])

    def add_ledger(self, e: LedgerEntry) -> int:
        with self._conn() as c:
            return self.insert_ledger_in(c, e)

    def list_ledger(self, month: str | None = None) -> list[LedgerEntry]:
        """Entries (newest first); ``month`` = "YYYY-MM" filters by created_at prefix."""
        with self._conn() as c:
            if month:
                rows = c.execute("SELECT * FROM ledger WHERE created_at LIKE ? ORDER BY id DESC",
                                 (f"{month}%",)).fetchall()
            else:
                rows = c.execute("SELECT * FROM ledger ORDER BY id DESC").fetchall()
        return [self._row_to_ledger(r) for r in rows]

    def month_totals(self, month: str) -> dict:
        """{"expense": x, "income": y, "net": y - x} for "YYYY-MM"."""
        totals = {"expense": 0.0, "income": 0.0}
        for e in self.list_ledger(month):
            totals[e.kind] = totals.get(e.kind, 0.0) + e.amount_ils
        expense, income = round(totals["expense"], 2), round(totals["income"], 2)
        return {"expense": expense, "income": income, "net": round(income - expense, 2)}

    # ------------------------------------------------------------------ events
    def log_event(self, actor: str, message: str, level: str = "info",
                  video_id: int | None = None) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO events (ts, actor, message, level, video_id) VALUES (?,?,?,?,?)",
                      (now_iso(), actor, message, level, video_id))

    def list_events(self, limit: int = 200) -> list[dict]:
        """Newest first."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ staff status
    def set_staff_status(self, name: str, status: str, detail: str = "") -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO staff_status (name, status, detail, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET status=excluded.status, detail=excluded.detail, "
                "updated_at=excluded.updated_at",
                (name, status, detail or "", now_iso()))

    def list_staff_status(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM staff_status ORDER BY name").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ key-value
    def kv_get(self, key: str, default: Any = None) -> Any:
        with self._conn() as c:
            r = c.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        if r is None:
            return default
        return _loads(r["value"], default)

    def kv_set(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO kv (key, value) VALUES (?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value, ensure_ascii=False, default=str)))
