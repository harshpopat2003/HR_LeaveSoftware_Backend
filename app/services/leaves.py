"""Leave requests: preview, create, edit, cancel, approve, reject; adjustments."""

from datetime import date
from decimal import Decimal

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import schemas as S
from app.clock import now_utc
from app.deps import Principal, ensure_company
from app.errors import conflict, invalid, not_found
from app.models import Employee, LeaveAdjustment, LeaveRequest, LeaveType
from app.rules import leave_rules as R
from app.services import audit, facts
from app.services.rules import for_company


def leave_type(db: Session, code: str, *, active_only: bool = True) -> LeaveType:
    lt = db.scalar(select(LeaveType).where(LeaveType.code == code))
    if lt is None or (active_only and not lt.is_active):
        raise invalid(f"Unknown leave type '{code}'.", {"field": "leaveType"})
    return lt


def get(db: Session, p: Principal, leave_id: int) -> LeaveRequest:
    lv = db.get(LeaveRequest, leave_id)
    if lv is None:
        raise not_found("Leave")
    ensure_company(p, lv.employee.company_id, "Leave")
    return lv


def validate(
    db: Session, e: Employee, code: str, start: date, end: date, exclude_id: int | None = None
) -> R.Validation:
    rules = for_company(db, e.company_id)
    b = facts.load(db, [e.id], tickets=False)[e.id]
    v = R.validate_leave(facts.emp_facts(e), code, start, end, b.leaves, rules, b.adjustments, exclude_id)
    if e.leaving_date and end > e.leaving_date:
        v.errors.append("The leave ends after the employee's leaving date.")
        v.ok = False
    if e.status != "active":
        v.warnings.append(f"This employee is marked {e.status}.")
    return v


def _flush_or_overlap(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        name = getattr(getattr(exc.orig, "diag", None), "constraint_name", "")
        if name == "ex_leave_requests_no_overlap":
            raise conflict("This leave overlaps another pending or approved leave of the same employee.") from exc
        raise


def create(db: Session, p: Principal, e: Employee, body: S.LeaveIn) -> tuple[LeaveRequest, list[str]]:
    lt = leave_type(db, body.leave_type)
    v = validate(db, e, lt.code, body.start_date, body.end_date)
    if not v.ok:
        raise invalid(" ".join(v.errors), {"errors": v.errors})
    lv = LeaveRequest(
        employee_id=e.id,
        leave_type_id=lt.id,
        start_date=body.start_date,
        end_date=body.end_date,
        days=v.days,
        status="pending",
        reason=body.reason,
        requested_by=p.id,
        created_by=p.id,
        updated_by=p.id,
    )
    db.add(lv)
    _flush_or_overlap(db)
    audit.record(db, p.id, "create", "leave_request", lv.id, e.company_id, after=audit.snap(lv))
    return lv, v.warnings


def update(db: Session, p: Principal, lv: LeaveRequest, body: S.LeavePatch) -> tuple[LeaveRequest, list[str]]:
    if lv.status != "pending":
        raise conflict(f"Only pending leaves can be edited. This leave is {lv.status}.")
    changes = body.model_dump(exclude_unset=True)
    for k in ("leave_type", "start_date", "end_date"):
        if k in changes and changes[k] is None:
            raise invalid(f"{k} cannot be empty.", {"field": k})
    before = audit.snap(lv)
    lt = leave_type(db, changes["leave_type"]) if "leave_type" in changes else lv.leave_type
    start = changes.get("start_date", lv.start_date)
    end = changes.get("end_date", lv.end_date)
    v = validate(db, lv.employee, lt.code, start, end, exclude_id=lv.id)
    if not v.ok:
        raise invalid(" ".join(v.errors), {"errors": v.errors})
    lv.leave_type_id, lv.start_date, lv.end_date, lv.days = lt.id, start, end, v.days
    if "reason" in changes:
        lv.reason = changes["reason"] or ""
    lv.updated_by = p.id
    _flush_or_overlap(db)
    db.refresh(lv)
    audit.record(
        db, p.id, "update", "leave_request", lv.id, lv.employee.company_id, before=before, after=audit.snap(lv)
    )
    return lv, v.warnings


def _decide(
    db: Session, p: Principal, lv: LeaveRequest, status: str, note: str, allowed_from: tuple[str, ...]
) -> LeaveRequest:
    if lv.status not in allowed_from:
        raise conflict(f"This leave is already {lv.status}.")
    before = audit.snap(lv)
    lv.status = status
    lv.decision_note = note
    lv.updated_by = p.id
    if status in ("approved", "rejected"):
        lv.decided_by = p.id
        lv.decided_at = now_utc()
    db.flush()
    db.refresh(lv)
    audit.record(
        db,
        p.id,
        {"approved": "approve", "rejected": "reject", "cancelled": "cancel"}[status],
        "leave_request",
        lv.id,
        lv.employee.company_id,
        before=before,
        after=audit.snap(lv),
    )
    return lv


def approve(db: Session, p: Principal, lv: LeaveRequest, note: str) -> LeaveRequest:
    return _decide(db, p, lv, "approved", note, ("pending",))


def reject(db: Session, p: Principal, lv: LeaveRequest, note: str) -> LeaveRequest:
    if not note.strip():
        raise invalid("Give a reason for rejecting.", {"field": "note"})
    return _decide(db, p, lv, "rejected", note, ("pending",))


def cancel(db: Session, p: Principal, lv: LeaveRequest, note: str) -> LeaveRequest:
    return _decide(db, p, lv, "cancelled", note, ("pending", "approved"))


def list_stmt(
    companies: list[int] | None,
    employee_id: int | None = None,
    status: str | None = None,
    type_code: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> Select[LeaveRequest]:
    stmt = select(LeaveRequest).join(Employee, Employee.id == LeaveRequest.employee_id)
    if companies is not None:
        stmt = stmt.where(Employee.company_id.in_(companies))
    if employee_id is not None:
        stmt = stmt.where(LeaveRequest.employee_id == employee_id)
    if status:
        stmt = stmt.where(LeaveRequest.status == status)
    if type_code:
        stmt = stmt.join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id).where(LeaveType.code == type_code)
    if date_from:
        stmt = stmt.where(LeaveRequest.end_date >= date_from)
    if date_to:
        stmt = stmt.where(LeaveRequest.start_date <= date_to)
    return stmt


def add_adjustment(db: Session, p: Principal, e: Employee, body: S.AdjustmentIn) -> LeaveAdjustment:
    lt = leave_type(db, body.leave_type)
    if body.effective_date < e.joining_date:
        raise invalid("The effective date is before the joining date.", {"field": "effectiveDate"})
    a = LeaveAdjustment(
        employee_id=e.id,
        leave_type_id=lt.id,
        days=Decimal(str(body.days)),
        effective_date=body.effective_date,
        reason=body.reason,
        created_by=p.id,
        updated_by=p.id,
    )
    db.add(a)
    db.flush()
    audit.record(db, p.id, "create", "leave_adjustment", a.id, e.company_id, after=audit.snap(a))
    return a


def adjustment_out(a: LeaveAdjustment) -> S.AdjustmentOut:
    return S.AdjustmentOut(
        id=a.id,
        employee_id=a.employee_id,
        leave_type=a.leave_type.code,
        days=float(a.days),
        effective_date=a.effective_date,
        reason=a.reason,
        created_at=a.created_at,
    )
