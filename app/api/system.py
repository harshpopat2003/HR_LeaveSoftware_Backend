from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app import __version__
from app.db import get_db
from app.errors import AppError

router = APIRouter(prefix="/health", tags=["system"])


@router.get("")
def health() -> dict[str, str]:
    """The process is up (no database check). For the tunnel or load balancer."""
    return {"status": "ok", "version": __version__}


@router.get("/ready")
def ready(db: Session = Depends(get_db)) -> dict[str, str]:
    """The database answers and the schema is migrated."""
    try:
        rev = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError as exc:
        raise AppError(503, "not_ready", "The database is not reachable or not migrated.") from exc
    return {"status": "ready", "migration": str(rev)}
