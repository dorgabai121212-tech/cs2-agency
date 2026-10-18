"""Activity feed: every staff action is written to the DB and to python logging."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .db import DB

log = logging.getLogger("agency")

_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
           "warn": logging.WARNING, "error": logging.ERROR, "success": logging.INFO}


def emit(db: "DB | None", actor: str, message: str, level: str = "info",
         video_id: int | None = None) -> None:
    """Record an event in the activity feed and log it. Never raises because of logging."""
    log.log(_LEVELS.get(level, logging.INFO), "[%s] %s", actor, message)
    if db is not None:
        try:
            db.log_event(actor, message, level=level, video_id=video_id)
        except Exception:  # pragma: no cover - feed must never crash the caller
            log.exception("failed to write event to DB")
