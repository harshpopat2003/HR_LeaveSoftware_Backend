from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import schemas as S
from app.db import get_db
from app.deps import PageParams, Principal, paging, require, scope
from app.models import AirTicket, Employee
from app.services import employees, present
from app.services import tickets as svc

router = APIRouter(tags=["tickets"])


@router.get("/employees/{employee_id}/tickets", response_model=S.EmployeeTickets)
def employee_tickets(
    employee_id: int, p: Principal = Depends(require("employees.view")), db: Session = Depends(get_db)
) -> S.EmployeeTickets:
    return svc.for_employee(db, employees.get(db, p, employee_id))


@router.post("/employees/{employee_id}/tickets", response_model=S.TicketOut, status_code=201)
def issue_ticket(
    employee_id: int, body: S.TicketIn, p: Principal = Depends(require("employees.edit")), db: Session = Depends(get_db)
) -> S.TicketOut:
    e = employees.get(db, p, employee_id)
    t = svc.issue(db, p, e, body)
    db.commit()
    hist = {h.id: h for h in svc.for_employee(db, e).history}
    return hist.get(t.id) or present.ticket_out(t, {p.id: p.user.full_name})


@router.patch("/tickets/{ticket_id}", response_model=S.TicketOut)
def update_ticket(
    ticket_id: int,
    body: S.TicketPatch,
    p: Principal = Depends(require("employees.edit")),
    db: Session = Depends(get_db),
) -> S.TicketOut:
    t, e = svc.get(db, p, ticket_id)
    svc.update(db, p, t, e, body)
    db.commit()
    hist = {h.id: h for h in svc.for_employee(db, e).history}
    return hist[t.id]


@router.get("/tickets", response_model=S.Page[S.TicketOut])
def list_tickets(
    company_id: int | None = None,
    employee_id: int | None = None,
    pp: PageParams = Depends(paging),
    p: Principal = Depends(require("employees.view")),
    db: Session = Depends(get_db),
) -> S.Page[S.TicketOut]:
    """Every issued ticket, newest first (the frontend works out early tickets from these)."""
    stmt = select(AirTicket).join(Employee, Employee.id == AirTicket.employee_id)
    companies = scope(p, company_id)
    if companies is not None:
        stmt = stmt.where(Employee.company_id.in_(companies))
    if employee_id is not None:
        stmt = stmt.where(AirTicket.employee_id == employee_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(
        db.scalars(stmt.order_by(AirTicket.issued_on.desc(), AirTicket.id.desc()).offset(pp.offset).limit(pp.size))
    )
    names = employees.user_names(db, {t.issued_by for t in rows})
    return S.Page(items=[present.ticket_out(t, names) for t in rows], total=total, page=pp.page, size=pp.size)


@router.get("/tickets/due", response_model=list[S.TicketDueRow])
def tickets_due(
    company_id: int | None = None,
    within_days: int | None = Query(None, ge=0, le=730),
    p: Principal = Depends(require("employees.view")),
    db: Session = Depends(get_db),
) -> list[S.TicketDueRow]:
    return svc.due(db, scope(p, company_id), within_days)


@router.get("/tickets/summary", response_model=S.TicketSummary)
def tickets_summary(
    company_id: int | None = None, p: Principal = Depends(require("employees.view")), db: Session = Depends(get_db)
) -> S.TicketSummary:
    return svc.summary(db, scope(p, company_id))
