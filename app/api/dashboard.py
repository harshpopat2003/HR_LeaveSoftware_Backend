from datetime import date

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import schemas as S
from app.clock import today
from app.db import get_db
from app.deps import Principal, require, scope
from app.services import dashboard as svc

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("", response_model=S.DashboardOut)
def dashboard(
    company_id: int | None = None, p: Principal = Depends(require("dashboard.view")), db: Session = Depends(get_db)
) -> S.DashboardOut:
    return svc.build(db, scope(p, company_id), company_id)


@router.get("/drill", response_model=S.DrillOut)
def drill(
    chart: str = Query(min_length=1, max_length=30),
    key: str = Query("", max_length=100),
    company_id: int | None = None,
    p: Principal = Depends(require("dashboard.view")),
    db: Session = Depends(get_db),
) -> S.DrillOut:
    return svc.drill(db, scope(p, company_id), chart, key)


@router.get("/on-leave", response_model=list[S.LeaveOut])
def on_leave(
    company_id: int | None = None,
    on: date | None = Query(None, alias="date"),
    p: Principal = Depends(require("dashboard.view")),
    db: Session = Depends(get_db),
) -> list[S.LeaveOut]:
    return svc.on_leave(db, scope(p, company_id), on or today())
