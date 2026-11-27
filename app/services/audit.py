"""Append-only audit trail. Every change made through the API writes one row."""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.deps import client_ip
from app.models import AuditLog

_HIDDEN = {"password_hash", "token_hash"}


def _plain(v: Any) -> Any:
    if isinstance(v, datetime | date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def snap(obj: Any) -> dict[str, Any]:
    """Column values of a row, safe to store as JSON (secrets left out)."""
    return {c.key: _plain(getattr(obj, c.key)) for c in inspect(obj).mapper.column_attrs if c.key not in _HIDDEN}


def record(
    db: Session,
    user_id: int | None,
    action: str,
    entity: str,
    entity_id: int | None = None,
    company_id: int | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    if before is not None and after is not None:
        changed = {k for k in after if before.get(k) != after.get(k)} - {"updated_at", "updated_by"}
        before = {k: before.get(k) for k in changed}
        after = {k: after[k] for k in changed}
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            entity=entity,
            entity_id=entity_id,
            company_id=company_id,
            before=before,
            after=after,
            ip=client_ip.get()[:64],
        )
    )
