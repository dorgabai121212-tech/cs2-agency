"""Shared base for every staff member, plus the Context object passed around the agency."""
from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings, load_settings
from ..db import DB
from ..events import emit
from ..llm import LLM


@dataclass
class Context:
    """Everything a staff member needs: settings, database and the LLM."""

    settings: Settings
    db: DB
    llm: LLM


def build_context(settings: Settings | None = None) -> Context:
    """Create a Context from ``settings`` (or load them from the current project)."""
    settings = settings or load_settings()
    return Context(settings=settings, db=DB(settings.db_path), llm=LLM(settings))


class StaffMember:
    """Base class. Subclasses set ``name``, ``title_he`` and ``role_prompt``."""

    name: str = "staff"
    title_he: str = "עובד"
    role_prompt: str = "You are a member of a small marketing agency for a Counter-Strike 2 creator."

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.settings = ctx.settings
        self.db = ctx.db
        self.llm = ctx.llm

    @property
    def language(self) -> str:
        """Primary channel language (e.g. "he")."""
        return self.settings.get("channel.language", "he")

    def status(self, s: str, detail: str = "", *, quiet: bool = False) -> None:
        """Update this member's live status (dashboard org chart) and the activity feed.

        A bare status with no detail (e.g. back to "idle") and ``quiet`` updates (the chat,
        the periodic stats pull) change the team row only, without an activity-feed line.
        """
        self.db.set_staff_status(self.name, s, detail)
        if detail and not quiet:
            emit(self.db, self.name, f"{s}: {detail}")

    def emit(self, message: str, level: str = "info", video_id: int | None = None) -> None:
        """Convenience: write to the activity feed as this member."""
        emit(self.db, self.name, message, level=level, video_id=video_id)
