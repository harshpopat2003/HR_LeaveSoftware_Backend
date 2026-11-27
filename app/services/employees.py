"""Employees: list, search, create, update, and the summary/stats views."""

from collections import Counter
from datetime import date, timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app import schemas as S
from app.clock import today
from app.deps import PageParams, Principal, ensure_company
from app.errors import conflict, invalid, not_found
from app.models import Department, Employee, EmployeeDocument, User
from app.rules import leave_rules as R
from app.services import audit, facts, present
from app.services.rules import RulesCache

SORTS = {
    "emp_no": Employee.emp_no,
    "name": Employee.first_name,
    "joining_date": Employee.joining_date,
    "department": Employee.department_id,
}


def get(db: Session, p: Principal, employee_id: int) -> Employee:
    e = db.get(Employee, employee_id)
    if e is None:
        raise not_found("Employee")
    ensure_company(p, e.company_id, "Employee")
    return e


def filtered(
    companies: list[int] | None,
    q: str | None = None,
    department_id: int | None = None,
    category: str | None = None,
    status: str | None = None,
) -> Select[Employee]:
    stmt = select(Employee)
    if companies is not None:
        stmt = stmt.where(Employee.company_id.in_(companies))
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Employee.first_name + " " + Employee.last_name).like(like),
                func.lower(Employee.emp_no).like(like),
                func.lower(Employee.designation).like(like),
            )
        )
    if department_id is not None:
        stmt = stmt.where(Employee.department_id == department_id)
    if category:
        stmt = stmt.where(Employee.category == category)
    if status:
        stmt = stmt.where(Employee.status == status)
    return stmt


def list_page(db: Session, stmt: Select[Employee], pp: PageParams, sort: str) -> tuple[list[Employee], int]:
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    desc = sort.startswith("-")
    col = SORTS.get(sort.lstrip("-"), Employee.emp_no)
    rows = db.scalars(stmt.order_by(col.desc() if desc else col, Employee.id).offset(pp.offset).limit(pp.size)).unique()
    return list(rows), total


def _check_department(db: Session, company_id: int, department_id: int) -> None:
    d = db.get(Department, department_id)
    if d is None or d.company_id != company_id:
        raise invalid("The department does not belong to this company.", {"field": "departmentId"})


def _check_emp_no(db: Session, emp_no: str, exclude: int | None = None) -> None:
    stmt = select(Employee.id).where(func.lower(Employee.emp_no) == emp_no.lower())
    if exclude:
        stmt = stmt.where(Employee.id != exclude)
    if db.scalar(stmt):
        raise conflict(f"Employee number {emp_no} is already used.", {"field": "empNo"})


def _check_dates(joining: date, leaving: date | None, status: str) -> None:
    if leaving and leaving < joining:
        raise invalid("The leaving date is before the joining date.", {"field": "leavingDate"})
    if status == "left" and not leaving:
        raise invalid("Enter the leaving date for an employee who has left.", {"field": "leavingDate"})


def create(db: Session, p: Principal, body: S.EmployeeCreate) -> Employee:
    ensure_company(p, body.company_id, "Company")
    _check_department(db, body.company_id, body.department_id)
    _check_emp_no(db, body.emp_no)
    _check_dates(body.joining_date, body.leaving_date, body.status)
    types = [d.doc_type for d in body.documents]
    if len(types) != len(set(types)):
        raise invalid("Each document type can be given once.", {"field": "documents"})
    data = body.model_dump(exclude={"documents"})
    e = Employee(**data, created_by=p.id, updated_by=p.id)
    e.documents = [EmployeeDocument(**d.model_dump(), created_by=p.id, updated_by=p.id) for d in body.documents]
    db.add(e)
    db.flush()
    audit.record(db, p.id, "create", "employee", e.id, e.company_id, after=audit.snap(e))
    return e


def update(db: Session, p: Principal, e: Employee, body: S.EmployeePatch) -> Employee:
    changes = body.model_dump(exclude_unset=True)
    for k in ("company_id", "department_id", "emp_no", "first_name", "category", "joining_date", "status"):
        if k in changes and changes[k] is None:
            raise invalid(f"{k} cannot be empty.", {"field": k})
    before = audit.snap(e)
    company_id = changes.get("company_id", e.company_id)
    department_id = changes.get("department_id", e.department_id)
    if "company_id" in changes:
        ensure_company(p, company_id, "Company")
    if "company_id" in changes or "department_id" in changes:
        _check_department(db, company_id, department_id)
    if "emp_no" in changes:
        _check_emp_no(db, changes["emp_no"], e.id)
    _check_dates(
        changes.get("joining_date", e.joining_date),
        changes.get("leaving_date", e.leaving_date),
        changes.get("status", e.status),
    )
    for k, v in changes.items():
        setattr(e, k, v)
    e.updated_by = p.id
    db.flush()
    db.refresh(e)
    audit.record(db, p.id, "update", "employee", e.id, e.company_id, before=before, after=audit.snap(e))
    return e


def last_and_next_leave(b: facts.Bundle, on: date) -> tuple[R.LeaveFacts | None, R.LeaveFacts | None]:
    approved = [lv for lv in b.leaves if lv.status == "approved"]
    past = sorted((lv for lv in approved if lv.start <= on), key=lambda lv: lv.start, reverse=True)
    future = sorted(
        (lv for lv in b.leaves if lv.status in ("approved", "pending") and lv.start > on), key=lambda lv: lv.start
    )
    return (past[0] if past else None), (future[0] if future else None)


def summary(db: Session, e: Employee) -> S.EmployeeSummary:
    t = today()
    rules = RulesCache(db)
    b = facts.load(db, [e.id])[e.id]
    r = rules.get(e.company_id)
    last, nxt = last_and_next_leave(b, t)
    last_annual = next(
        (
            lv
            for lv in sorted(b.leaves, key=lambda x: x.start, reverse=True)
            if lv.type_code == "annual" and lv.status == "approved" and lv.start <= t
        ),
        None,
    )
    return S.EmployeeSummary(
        employee=present.employee_out(e, t, rules),
        balances=[present.balance_out(x) for x in facts.balances(e, b, t, rules)],
        ticket=present.ticket_info_out(facts.ticket(e, b, t, rules)),
        last_leave=present.leave_brief(last, R.leave_days(last.start, last.end, r)) if last else None,
        next_leave=present.leave_brief(nxt, R.leave_days(nxt.start, nxt.end, r)) if nxt else None,
        next_leave_plan=R.next_leave_plan(e.category, last_annual.end, r) if last_annual else None,
    )


def search(db: Session, companies: list[int] | None, q: str, limit: int = 10) -> list[S.SearchHit]:
    t = today()
    rules = RulesCache(db)
    term = q.strip().lower()
    name = func.lower(Employee.first_name + " " + Employee.last_name)
    stmt = (
        filtered(companies, q=term)
        .order_by((func.lower(Employee.emp_no) == term).desc(), func.similarity(name, term).desc(), Employee.emp_no)
        .limit(limit)
    )
    emps = list(db.scalars(stmt).unique())
    if not emps and len(term) >= 3:  # nothing contains the text: fall back to similar-sounding names (typos)
        sim = select(Employee).where(func.similarity(name, term) > 0.25)
        if companies is not None:
            sim = sim.where(Employee.company_id.in_(companies))
        emps = list(db.scalars(sim.order_by(func.similarity(name, term).desc()).limit(limit)).unique())
    bundles = facts.load(db, [e.id for e in emps])
    out = []
    for e in emps:
        b = bundles[e.id]
        r = rules.get(e.company_id)
        last, _ = last_and_next_leave(b, t)
        annual = next((x for x in facts.balances(e, b, t, rules) if x.type_code == "annual"), None)
        out.append(
            S.SearchHit(
                id=e.id,
                emp_no=e.emp_no,
                full_name=e.full_name,
                company=present.company_ref(e.company),
                department=e.department.name,
                designation=e.designation,
                category=e.category,
                status=e.status,
                last_leave=present.leave_brief(last, R.leave_days(last.start, last.end, r)) if last else None,
                annual_remaining=annual.remaining if annual else None,
                ticket=present.ticket_info_out(facts.ticket(e, b, t, rules)),
            )
        )
    return out


def stats(db: Session, e: Employee) -> S.EmployeeStats:
    t = today()
    r = RulesCache(db).get(e.company_id)
    b = facts.load(db, [e.id], adjustments=False, tickets=False)[e.id]
    approved = [lv for lv in b.leaves if lv.status == "approved"]
    years: dict[int, Counter[str]] = {}
    for y in range(e.joining_date.year, t.year + 1):
        years[y] = Counter()
    for lv in approved:
        for y in range(lv.start.year, lv.end.year + 1):
            n = R.overlap_days(lv.start, lv.end, date(y, 1, 1), date(y, 12, 31), r)
            years.setdefault(y, Counter())[lv.type_code] += n
    months: list[S.MonthStat] = []
    y, m = t.year, t.month
    for _ in range(12):
        start = date(y, m, 1)
        end = date(y + (m == 12), m % 12 + 1, 1)
        last = end - timedelta(days=1)
        months.append(
            S.MonthStat(
                month=f"{y:04d}-{m:02d}", days=sum(R.overlap_days(lv.start, lv.end, start, last, r) for lv in approved)
            )
        )
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    by_type: Counter[str] = Counter()
    for lv in approved:
        by_type[lv.type_code] += R.leave_days(lv.start, lv.end, r)
    return S.EmployeeStats(
        by_year=[
            S.YearStat(year=y, annual=c["annual"], emergency=c["emergency"], other=c["other"])
            for y, c in sorted(years.items())
        ],
        last_12_months=list(reversed(months)),
        by_type=dict(by_type),
        by_status=dict(Counter(lv.status for lv in b.leaves)),
    )


def user_names(db: Session, ids: set[int | None]) -> dict[int, str]:
    clean = {i for i in ids if i}
    if not clean:
        return {}
    return dict(db.execute(select(User.id, User.full_name).where(User.id.in_(clean))).tuples().all())
