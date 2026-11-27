from typing import Literal

from fastapi import APIRouter, Depends, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import schemas as S
from app.clock import today
from app.db import get_db
from app.deps import PageParams, Principal, paging, require, scope
from app.services import documents as svc
from app.services import employees, present, tickets
from app.services.rules import RulesCache

router = APIRouter(tags=["documents"])

DocPath = Literal["passport", "emirates_id", "visit_visa"]
Level = Literal["expired", "urgent", "soon", "ok"]


@router.get("/employees/{employee_id}/documents", response_model=list[S.DocumentOut])
def employee_documents(
    employee_id: int, p: Principal = Depends(require("documents.view")), db: Session = Depends(get_db)
) -> list[S.DocumentOut]:
    return present.employee_out(employees.get(db, p, employee_id), today(), RulesCache(db)).documents


@router.put("/employees/{employee_id}/documents/{doc_type}", response_model=S.DocumentOut | None)
def put_document(
    employee_id: int,
    doc_type: DocPath,
    body: S.DocumentPut,
    p: Principal = Depends(require("documents.edit")),
    db: Session = Depends(get_db),
) -> S.DocumentOut | Response:
    e = employees.get(db, p, employee_id)
    doc = svc.put(db, p, e, doc_type, body)
    db.commit()
    if doc is None:
        return Response(status_code=204)
    return present.document_out(doc, today(), RulesCache(db).get(e.company_id))


@router.post("/employees/{employee_id}/documents/{doc_type}/renew", response_model=S.RenewalOut, status_code=201)
def renew_document(
    employee_id: int,
    doc_type: DocPath,
    body: S.DocumentRenewIn,
    p: Principal = Depends(require("documents.edit")),
    db: Session = Depends(get_db),
) -> S.RenewalOut:
    e = employees.get(db, p, employee_id)
    _, ren = svc.renew(db, p, e, doc_type, body)
    db.commit()
    db.refresh(ren)
    return present.renewal_out(ren, {p.id: p.user.full_name})


@router.get("/documents", response_model=S.Page[S.DocumentRow])
def list_documents(
    company_id: int | None = None,
    doc_type: DocPath | None = None,
    level: Level | None = None,
    pp: PageParams = Depends(paging),
    p: Principal = Depends(require("documents.view")),
    db: Session = Depends(get_db),
) -> S.Page[S.DocumentRow]:
    rows = svc.rows(db, scope(p, company_id), doc_type, (level,) if level else None)
    return S.Page(items=rows[pp.offset : pp.offset + pp.size], total=len(rows), page=pp.page, size=pp.size)


@router.get("/documents/alerts", response_model=list[S.DocumentRow])
def document_alerts(
    company_id: int | None = None, p: Principal = Depends(require("documents.view")), db: Session = Depends(get_db)
) -> list[S.DocumentRow]:
    return svc.rows(db, scope(p, company_id), levels=svc.ALERT_LEVELS)


@router.get("/documents/summary", response_model=list[S.DocumentsSummaryRow])
def document_summary(
    company_id: int | None = None, p: Principal = Depends(require("documents.view")), db: Session = Depends(get_db)
) -> list[S.DocumentsSummaryRow]:
    return svc.summary(db, scope(p, company_id))


@router.get("/documents/renewals", response_model=S.Page[S.RenewalOut])
def document_renewals(
    company_id: int | None = None,
    employee_id: int | None = None,
    pp: PageParams = Depends(paging),
    p: Principal = Depends(require("documents.view")),
    db: Session = Depends(get_db),
) -> S.Page[S.RenewalOut]:
    if employee_id is not None:
        employees.get(db, p, employee_id)
    stmt = svc.renewals(db, scope(p, company_id), employee_id)
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    rows = list(db.scalars(stmt.offset(pp.offset).limit(pp.size)).unique())
    names = employees.user_names(db, {r.renewed_by for r in rows})
    return S.Page(items=[present.renewal_out(r, names) for r in rows], total=total, page=pp.page, size=pp.size)


@router.get("/reminders/today", response_model=S.ReminderOut)
def reminders_today(p: Principal = Depends(require("documents.view")), db: Session = Depends(get_db)) -> S.ReminderOut:
    """The once-a-day pop-up: documents expired or expiring very soon, and tickets overdue or due soon."""
    t = today()
    companies = scope(p, None)
    docs = svc.rows(db, companies, levels=("expired", "urgent"))
    due = [r for r in tickets.due(db, companies) if r.overdue or (r.days_left is not None and r.days_left <= 30)]
    return S.ReminderOut(date=t, seen=svc.reminder_seen(db, p.id, t), documents=docs, tickets=due)


@router.post("/reminders/today/seen", status_code=204)
def reminders_seen(p: Principal = Depends(require("documents.view")), db: Session = Depends(get_db)) -> Response:
    svc.mark_seen(db, p.id, today())
    db.commit()
    return Response(status_code=204)
