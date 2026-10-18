"""Plain dataclasses shared by every part of the agency. All are JSON-serialisable via to_dict()."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def now_iso() -> str:
    """Current UTC time as ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class VideoStatus(str, Enum):
    """Lifecycle of a video inside the agency."""

    NEW = "new"
    ANALYZED = "analyzed"
    EDITED = "edited"
    PLANNED = "planned"
    AWAITING_APPROVAL = "awaiting_approval"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class _Serializable:
    def to_dict(self) -> dict:
        """JSON-friendly dict (enums become their values)."""
        return _jsonable(asdict(self))  # type: ignore[arg-type]

    @classmethod
    def from_dict(cls, data: dict):
        """Build from a dict, ignoring unknown keys."""
        names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass
class Video(_Serializable):
    id: int | None
    path: str
    title_hint: str = ""
    status: VideoStatus = VideoStatus.NEW
    created_at: str = field(default_factory=now_iso)
    analysis: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


@dataclass
class Variant(_Serializable):
    platform: str
    path: str
    aspect: str = "16:9"
    duration_s: float | None = None


@dataclass
class PostPlan(_Serializable):
    platform: str
    variant_path: str
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    scheduled_at: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class PostResult(_Serializable):
    platform: str
    ok: bool
    dry_run: bool = False
    external_id: str | None = None
    url: str | None = None
    error: str | None = None


@dataclass
class Approval(_Serializable):
    id: int | None
    kind: str  # "publish" | "spend" | "reply" | "other"
    title: str
    payload: dict = field(default_factory=dict)
    cost_ils: float = 0.0
    status: str = "pending"  # "pending" | "approved" | "executing" (claimed) | "rejected" | "executed"
    requested_by: str = ""
    created_at: str = field(default_factory=now_iso)
    decided_at: str | None = None
    note: str = ""


@dataclass
class LedgerEntry(_Serializable):
    id: int | None
    kind: str  # "expense" | "income"
    amount_ils: float
    category: str = ""
    description: str = ""
    platform: str | None = None
    video_id: int | None = None
    created_at: str = field(default_factory=now_iso)
