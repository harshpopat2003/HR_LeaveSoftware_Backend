"""Today's date in Asia/Dubai. Kept in one place so tests can freeze it."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from app.config import get_settings

_frozen: date | None = None


def today() -> date:
    if _frozen is not None:
        return _frozen
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def now_utc() -> datetime:
    return datetime.now(UTC)


def freeze(d: date | None) -> None:
    """Tests only: pin today's date (None to unpin)."""
    global _frozen
    _frozen = d
