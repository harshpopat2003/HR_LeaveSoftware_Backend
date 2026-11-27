"""Report data. The Excel files are built in the browser from these rows (the layout lives in the frontend)."""

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schemas as S
from app.clock import now_utc, today
from app.db import get_db
from app.deps import Principal, require, scope
from app.models import AirTicket, Employee, LeaveRequest
from app.rules import leave_rules as R
from app.services import documents, employees, facts, leaves, present
from app.services.rules import RulesCache

router = APIRouter(prefix="/reports", tags=["reports"])

EmpStatus = Literal["active", "inactive", "left"]


def _employees(db: Session, companies: list[int] | None, status: str | None) -> list[Employee]:
    return list(
        db.scalars(employees.filtered(companies, status=status).order_by(Employee.company_id, Employee.emp_no)).unique()
    )


@router.get("/employees", response_model=list[S.EmployeeOut])
def report_employees(
    company_id: int | None = None,
    status: EmpStatus | None = None,
    p: Principal = Depends(require("data.export")),
    db: Session = Depends(get_db),
) -> list[S.EmployeeOut]:
    rules, t = RulesCache(db), today()
    return [present.employee_out(e, t, rules) for e in _employees(db, scope(p, company_id), status)]


@router.get("/leaves", response_model=list[S.LeaveOut])
def report_leaves(
    company_id: int | None = None,
    status: Literal["pending", "approved", "rejected", "cancelled"] | None = None,
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    p: Principal = Depends(require("data.export")),
    db: Session = Depends(get_db),
) -> list[S.LeaveOut]:
    stmt = leaves.list_stmt(scope(p, company_id), status=status, date_from=date_from, date_to=date_to)
    return [
        present.leave_out(lv) for lv in db.scalars(stmt.order_by(LeaveRequest.start_date, LeaveRequest.id)).unique()
    ]


@router.get("/documents", response_model=list[S.DocumentRow])
def report_documents(
    company_id: int | None = None, p: Principal = Depends(require("data.export")), db: Session = Depends(get_db)
) -> list[S.DocumentRow]:
    return documents.rows(db, scope(p, company_id))


@router.get("/leave-history", response_model=list[S.LeaveHistoryRow])
def report_leave_history(
    company_id: int | None = None,
    status: EmpStatus | None = "active",
    p: Principal = Depends(require("data.export")),
    db: Session = Depends(get_db),
) -> list[S.LeaveHistoryRow]:
    """One row per employee with every approved leave in date order, like the HR leave tracker sheet."""
    t = today()
    rules = RulesCache(db)
    emps = _employees(db, scope(p, company_id), status)
    bundles = facts.load(db, [e.id for e in emps])
    out = []
    for e in emps:
        b = bundles[e.id]
        r = rules.get(e.company_id)
        approved = sorted((lv for lv in b.leaves if lv.status == "approved"), key=lambda lv: lv.start)
        last_annual = next((lv for lv in reversed(approved) if lv.type_code == "annual" and lv.start <= t), None)
        out.append(
            S.LeaveHistoryRow(
                employee=present.employee_out(e, t, rules),
                leaves=[present.leave_brief(lv, R.leave_days(lv.start, lv.end, r)) for lv in approved],
                balances=[present.balance_out(x) for x in facts.balances(e, b, t, rules)],
                ticket=present.ticket_info_out(facts.ticket(e, b, t, rules)),
                next_leave_plan=R.next_leave_plan(e.category, last_annual.end, r) if last_annual else None,
            )
        )
    return out


@router.get("/full", response_model=S.FullReport)
def report_full(
    company_id: int | None = None, p: Principal = Depends(require("data.export")), db: Session = Depends(get_db)
) -> S.FullReport:
    """Everything for the full workbook: employees, leaves, documents, tickets and renewals."""
    companies = scope(p, company_id)
    rules, t = RulesCache(db), today()
    emps = _employees(db, companies, None)
    ids = [e.id for e in emps]
    lv_rows = db.scalars(leaves.list_stmt(companies).order_by(LeaveRequest.start_date, LeaveRequest.id)).unique()
    tk = (
        list(
            db.scalars(
                select(AirTicket)
                .where(AirTicket.employee_id.in_(ids))
                .order_by(AirTicket.employee_id, AirTicket.issued_on)
            )
        )
        if ids
        else []
    )
    ren = list(db.scalars(documents.renewals(db, companies, None)).unique())
    names = employees.user_names(db, {x.issued_by for x in tk} | {x.renewed_by for x in ren})
    return S.FullReport(
        generated_at=now_utc(),
        employees=[present.employee_out(e, t, rules) for e in emps],
        leaves=[present.leave_out(lv) for lv in lv_rows],
        documents=documents.rows(db, companies),
        tickets=[present.ticket_out(x, names) for x in tk],
        renewals=[present.renewal_out(x, names) for x in ren],
    )
