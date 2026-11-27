"""Company air tickets: history with early-ticket flags, due list and summary."""

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schemas as S
from app.clock import today
from app.deps import Principal, ensure_company
from app.errors import conflict, invalid, not_found
from app.models import AirTicket, Employee
from app.rules import leave_rules as R
from app.services import audit, facts, present
from app.services.employees import user_names
from app.services.rules import RulesCache


def get(db: Session, p: Principal, ticket_id: int) -> tuple[AirTicket, Employee]:
    t = db.get(AirTicket, ticket_id)
    if t is None:
        raise not_found("Ticket")
    e = db.get(Employee, t.employee_id)
    assert e is not None  # noqa: S101 - foreign key guarantees it
    ensure_company(p, e.company_id, "Ticket")
    return t, e


def for_employee(db: Session, e: Employee) -> S.EmployeeTickets:
    rules = RulesCache(db)
    rows = list(db.scalars(select(AirTicket).where(AirTicket.employee_id == e.id).order_by(AirTicket.issued_on.desc())))
    dates = [t.issued_on for t in rows]
    hist = {h.issued_on: h for h in R.ticket_history(facts.emp_facts(e), dates, rules.get(e.company_id))}
    names = user_names(db, {t.issued_by for t in rows})
    info = R.ticket_info(facts.emp_facts(e), dates, today(), rules.get(e.company_id))
    return S.EmployeeTickets(
        info=present.ticket_info_out(info), history=[present.ticket_out(t, names, hist.get(t.issued_on)) for t in rows]
    )


def _check(db: Session, e: Employee, issued_on: date, exclude: int | None = None) -> None:
    if not e.ticket_eligible:
        raise invalid("This employee is not eligible for a company ticket.")
    if issued_on < e.joining_date:
        raise invalid("The ticket date is before the joining date.", {"field": "issuedOn"})
    if issued_on > today():
        raise invalid("The ticket date cannot be in the future.", {"field": "issuedOn"})
    stmt = select(AirTicket.id).where(AirTicket.employee_id == e.id, AirTicket.issued_on == issued_on)
    if exclude:
        stmt = stmt.where(AirTicket.id != exclude)
    if db.scalar(stmt):
        raise conflict("A ticket is already recorded on this date for this employee.")


def issue(db: Session, p: Principal, e: Employee, body: S.TicketIn) -> AirTicket:
    _check(db, e, body.issued_on)
    t = AirTicket(
        employee_id=e.id,
        issued_on=body.issued_on,
        destination=body.destination,
        note=body.note,
        issued_by=p.id,
        created_by=p.id,
        updated_by=p.id,
    )
    db.add(t)
    db.flush()
    audit.record(db, p.id, "create", "air_ticket", t.id, e.company_id, after=audit.snap(t))
    return t


def update(db: Session, p: Principal, t: AirTicket, e: Employee, body: S.TicketPatch) -> AirTicket:
    changes = body.model_dump(exclude_unset=True)
    if "issued_on" in changes:
        if changes["issued_on"] is None:
            raise invalid("issuedOn cannot be empty.", {"field": "issuedOn"})
        _check(db, e, changes["issued_on"], exclude=t.id)
    before = audit.snap(t)
    for k, v in changes.items():
        setattr(t, k, v if v is not None else "")
    t.updated_by = p.id
    db.flush()
    audit.record(db, p.id, "update", "air_ticket", t.id, e.company_id, before=before, after=audit.snap(t))
    return t


def _eligible(db: Session, companies: list[int] | None) -> list[Employee]:
    stmt = select(Employee).where(Employee.status == "active", Employee.ticket_eligible.is_(True))
    if companies is not None:
        stmt = stmt.where(Employee.company_id.in_(companies))
    return list(db.scalars(stmt.order_by(Employee.emp_no)).unique())


def due(db: Session, companies: list[int] | None, within_days: int | None = None) -> list[S.TicketDueRow]:
    t = today()
    rules = RulesCache(db)
    emps = _eligible(db, companies)
    bundles = facts.load(db, [e.id for e in emps], leaves=False, adjustments=False)
    out = []
    for e in emps:
        info = facts.ticket(e, bundles[e.id], t, rules)
        window = within_days if within_days is not None else rules.get(e.company_id).ticketDueWarningDays
        if info.next_due is None:
            continue
        left = (info.next_due - t).days
        if info.overdue or left <= window:
            out.append(
                S.TicketDueRow(
                    employee_id=e.id,
                    emp_no=e.emp_no,
                    employee_name=e.full_name,
                    company=present.company_ref(e.company),
                    department=e.department.name,
                    category=e.category,
                    next_due=info.next_due,
                    days_left=left,
                    overdue=info.overdue,
                    last_issued=info.last_issued,
                )
            )
    return sorted(out, key=lambda r: (r.days_left if r.days_left is not None else 0, r.emp_no))


def summary(db: Session, companies: list[int] | None) -> S.TicketSummary:
    t = today()
    rules = RulesCache(db)
    emps = _eligible(db, companies)
    bundles = facts.load(db, [e.id for e in emps], leaves=False, adjustments=False)
    due_soon = overdue = issued = early = 0
    for e in emps:
        b = bundles[e.id]
        info = facts.ticket(e, b, t, rules)
        due_soon += info.due_soon and not info.overdue
        overdue += info.overdue
        for h in R.ticket_history(facts.emp_facts(e), b.tickets, rules.get(e.company_id)):
            if h.issued_on.year == t.year:
                issued += 1
                early += h.early
    return S.TicketSummary(
        eligible=len(emps), due_soon=due_soon, overdue=overdue, issued_this_year=issued, early_this_year=early
    )
