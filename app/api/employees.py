from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import schemas as S
from app.clock import today
from app.db import get_db
from app.deps import PageParams, Principal, paging, require, scope
from app.services import employees as svc
from app.services import facts, present
from app.services.rules import RulesCache

router = APIRouter(prefix="/employees", tags=["employees"])


@router.get("", response_model=S.Page[S.EmployeeOut])
def list_employees(
    company_id: int | None = None,
    q: str | None = Query(None, max_length=100),
    department_id: int | None = None,
    category: str | None = Query(None, pattern="^[AB]$"),
    status: str | None = Query(None, pattern="^(active|inactive|left)$"),
    sort: str = Query("emp_no", pattern="^-?(emp_no|name|joining_date|department)$"),
    pp: PageParams = Depends(paging),
    p: Principal = Depends(require("employees.view")),
    db: Session = Depends(get_db),
) -> S.Page[S.EmployeeOut]:
    stmt = svc.filtered(scope(p, company_id), q, department_id, category, status)
    rows, total = svc.list_page(db, stmt, pp, sort)
    rules, t = RulesCache(db), today()
    return S.Page(items=[present.employee_out(e, t, rules) for e in rows], total=total, page=pp.page, size=pp.size)


@router.get("/search", response_model=list[S.SearchHit])
def search(
    q: str = Query(min_length=1, max_length=100),
    company_id: int | None = None,
    limit: int = Query(10, ge=1, le=50),
    p: Principal = Depends(require("employees.view")),
    db: Session = Depends(get_db),
) -> list[S.SearchHit]:
    return svc.search(db, scope(p, company_id), q, limit)


@router.post("", response_model=S.EmployeeOut, status_code=201)
def create_employee(
    body: S.EmployeeCreate, p: Principal = Depends(require("employees.edit")), db: Session = Depends(get_db)
) -> S.EmployeeOut:
    e = svc.create(db, p, body)
    db.commit()
    db.refresh(e)
    return present.employee_out(e, today(), RulesCache(db))


@router.get("/{employee_id}", response_model=S.EmployeeOut)
def get_employee(
    employee_id: int, p: Principal = Depends(require("employees.view")), db: Session = Depends(get_db)
) -> S.EmployeeOut:
    return present.employee_out(svc.get(db, p, employee_id), today(), RulesCache(db))


@router.patch("/{employee_id}", response_model=S.EmployeeOut)
def update_employee(
    employee_id: int,
    body: S.EmployeePatch,
    p: Principal = Depends(require("employees.edit")),
    db: Session = Depends(get_db),
) -> S.EmployeeOut:
    e = svc.update(db, p, svc.get(db, p, employee_id), body)
    db.commit()
    return present.employee_out(e, today(), RulesCache(db))


@router.get("/{employee_id}/summary", response_model=S.EmployeeSummary)
def employee_summary(
    employee_id: int, p: Principal = Depends(require("employees.view")), db: Session = Depends(get_db)
) -> S.EmployeeSummary:
    return svc.summary(db, svc.get(db, p, employee_id))


@router.get("/{employee_id}/balances", response_model=list[S.BalanceOut])
def employee_balances(
    employee_id: int, p: Principal = Depends(require("leaves.view")), db: Session = Depends(get_db)
) -> list[S.BalanceOut]:
    e = svc.get(db, p, employee_id)
    b = facts.load(db, [e.id], tickets=False)[e.id]
    return [present.balance_out(x) for x in facts.balances(e, b, today(), RulesCache(db))]


@router.get("/{employee_id}/stats", response_model=S.EmployeeStats)
def employee_stats(
    employee_id: int, p: Principal = Depends(require("employees.view")), db: Session = Depends(get_db)
) -> S.EmployeeStats:
    return svc.stats(db, svc.get(db, p, employee_id))
