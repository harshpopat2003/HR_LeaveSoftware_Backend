from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import schemas as S
from app.db import get_db
from app.deps import PageParams, Principal, current_principal, paging, require, scope
from app.errors import conflict, not_found
from app.models import LeaveAdjustment, LeaveRequest, LeaveType
from app.services import audit, employees, facts, present
from app.services import leaves as svc
from app.services.rules import RulesCache

router = APIRouter(tags=["leaves"])

Status = Literal["pending", "approved", "rejected", "cancelled"]


# ---------------------------------------------------------------- leave types
@router.get("/leave-types", response_model=list[S.LeaveTypeOut])
def list_leave_types(
    include_inactive: bool = False, _: Principal = Depends(current_principal), db: Session = Depends(get_db)
) -> list[S.LeaveTypeOut]:
    q = select(LeaveType).order_by(LeaveType.id)
    if not include_inactive:
        q = q.where(LeaveType.is_active.is_(True))
    return [S.LeaveTypeOut.model_validate(x) for x in db.scalars(q)]


@router.post("/leave-types", response_model=S.LeaveTypeOut, status_code=201)
def create_leave_type(
    body: S.LeaveTypeIn, p: Principal = Depends(require("rules.edit")), db: Session = Depends(get_db)
) -> S.LeaveTypeOut:
    lt = LeaveType(code=body.code, name=body.name, created_by=p.id, updated_by=p.id)
    db.add(lt)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise conflict(f"Leave type '{body.code}' already exists.") from exc
    audit.record(db, p.id, "create", "leave_type", lt.id, after=audit.snap(lt))
    db.commit()
    return S.LeaveTypeOut.model_validate(lt)


@router.patch("/leave-types/{leave_type_id}", response_model=S.LeaveTypeOut)
def update_leave_type(
    leave_type_id: int,
    body: S.LeaveTypePatch,
    p: Principal = Depends(require("rules.edit")),
    db: Session = Depends(get_db),
) -> S.LeaveTypeOut:
    lt = db.get(LeaveType, leave_type_id)
    if lt is None:
        raise not_found("Leave type")
    if body.is_active is False and lt.code == "annual":
        raise conflict("Annual leave cannot be switched off.")
    before = audit.snap(lt)
    for k, v in body.model_dump(exclude_unset=True, exclude_none=True).items():
        setattr(lt, k, v)
    lt.updated_by = p.id
    db.flush()
    audit.record(db, p.id, "update", "leave_type", lt.id, before=before, after=audit.snap(lt))
    db.commit()
    return S.LeaveTypeOut.model_validate(lt)


# ---------------------------------------------------------------- leaves
def _page(db: Session, stmt: Select[LeaveRequest], pp: PageParams, newest_first: bool = True) -> S.Page[S.LeaveOut]:
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    order = (
        (LeaveRequest.start_date.desc(), LeaveRequest.id.desc())
        if newest_first
        else (LeaveRequest.start_date, LeaveRequest.id)
    )
    rows = db.scalars(stmt.order_by(*order).offset(pp.offset).limit(pp.size)).unique()
    return S.Page(items=[present.leave_out(lv) for lv in rows], total=total, page=pp.page, size=pp.size)


@router.get("/leaves", response_model=S.Page[S.LeaveOut])
def list_leaves(
    company_id: int | None = None,
    employee_id: int | None = None,
    status: Status | None = None,
    leave_type: str | None = Query(None, max_length=30),
    date_from: date | None = Query(None, alias="from"),
    date_to: date | None = Query(None, alias="to"),
    pp: PageParams = Depends(paging),
    p: Principal = Depends(require("leaves.view")),
    db: Session = Depends(get_db),
) -> S.Page[S.LeaveOut]:
    return _page(db, svc.list_stmt(scope(p, company_id), employee_id, status, leave_type, date_from, date_to), pp)


@router.post("/leaves/preview", response_model=S.ValidationOut)
def preview_leave(
    body: S.LeavePreviewIn, p: Principal = Depends(require("leaves.edit")), db: Session = Depends(get_db)
) -> S.ValidationOut:
    """Checks a leave without saving it: day count, overlaps and balance warnings."""
    e = employees.get(db, p, body.employee_id)
    lt = svc.leave_type(db, body.leave_type)
    v = svc.validate(db, e, lt.code, body.start_date, body.end_date, body.exclude_id)
    return S.ValidationOut(ok=v.ok, errors=v.errors, warnings=v.warnings, days=v.days)


@router.post("/leaves", response_model=S.LeaveSaved, status_code=201)
def create_leave(
    body: S.LeaveIn, p: Principal = Depends(require("leaves.edit")), db: Session = Depends(get_db)
) -> S.LeaveSaved:
    e = employees.get(db, p, body.employee_id)
    lv, warnings = svc.create(db, p, e, body)
    db.commit()
    db.refresh(lv)
    return S.LeaveSaved(leave=present.leave_out(lv), warnings=warnings)


@router.get("/leaves/{leave_id}", response_model=S.LeaveOut)
def get_leave(
    leave_id: int, p: Principal = Depends(require("leaves.view")), db: Session = Depends(get_db)
) -> S.LeaveOut:
    return present.leave_out(svc.get(db, p, leave_id))


@router.patch("/leaves/{leave_id}", response_model=S.LeaveSaved)
def update_leave(
    leave_id: int, body: S.LeavePatch, p: Principal = Depends(require("leaves.edit")), db: Session = Depends(get_db)
) -> S.LeaveSaved:
    lv, warnings = svc.update(db, p, svc.get(db, p, leave_id), body)
    db.commit()
    return S.LeaveSaved(leave=present.leave_out(lv), warnings=warnings)


@router.post("/leaves/{leave_id}/cancel", response_model=S.LeaveOut)
def cancel_leave(
    leave_id: int,
    body: S.DecisionIn | None = None,
    p: Principal = Depends(require("leaves.edit")),
    db: Session = Depends(get_db),
) -> S.LeaveOut:
    lv = svc.cancel(db, p, svc.get(db, p, leave_id), body.note if body else "")
    db.commit()
    return present.leave_out(lv)


@router.post("/leaves/{leave_id}/approve", response_model=S.LeaveOut)
def approve_leave(
    leave_id: int,
    body: S.DecisionIn | None = None,
    p: Principal = Depends(require("leaves.approve")),
    db: Session = Depends(get_db),
) -> S.LeaveOut:
    lv = svc.approve(db, p, svc.get(db, p, leave_id), body.note if body else "")
    db.commit()
    return present.leave_out(lv)


@router.post("/leaves/{leave_id}/reject", response_model=S.LeaveOut)
def reject_leave(
    leave_id: int, body: S.RejectIn, p: Principal = Depends(require("leaves.approve")), db: Session = Depends(get_db)
) -> S.LeaveOut:
    lv = svc.reject(db, p, svc.get(db, p, leave_id), body.note)
    db.commit()
    return present.leave_out(lv)


@router.get("/approvals", response_model=list[S.ApprovalRow])
def approvals(
    company_id: int | None = None, p: Principal = Depends(require("leaves.approve")), db: Session = Depends(get_db)
) -> list[S.ApprovalRow]:
    """Pending leaves, oldest start first, each with the balance it would use."""
    rows = list(
        db.scalars(
            svc.list_stmt(scope(p, company_id), status="pending").order_by(LeaveRequest.start_date, LeaveRequest.id)
        ).unique()
    )
    bundles = facts.load(db, [lv.employee_id for lv in rows], tickets=False)
    rules = RulesCache(db)
    out = []
    for lv in rows:
        bal = next(
            (
                b
                for b in facts.balances(lv.employee, bundles[lv.employee_id], lv.start_date, rules)
                if b.type_code == lv.leave_type.code
            ),
            None,
        )
        out.append(S.ApprovalRow(leave=present.leave_out(lv), balance=present.balance_out(bal) if bal else None))
    return out


@router.get("/employees/{employee_id}/leaves", response_model=S.Page[S.LeaveOut])
def employee_leaves(
    employee_id: int,
    status: Status | None = None,
    pp: PageParams = Depends(paging),
    p: Principal = Depends(require("leaves.view")),
    db: Session = Depends(get_db),
) -> S.Page[S.LeaveOut]:
    e = employees.get(db, p, employee_id)
    return _page(db, svc.list_stmt(None, e.id, status), pp)


# ---------------------------------------------------------------- adjustments
@router.get("/employees/{employee_id}/adjustments", response_model=list[S.AdjustmentOut])
def list_adjustments(
    employee_id: int, p: Principal = Depends(require("leaves.view")), db: Session = Depends(get_db)
) -> list[S.AdjustmentOut]:
    e = employees.get(db, p, employee_id)
    rows = db.scalars(
        select(LeaveAdjustment)
        .where(LeaveAdjustment.employee_id == e.id)
        .order_by(LeaveAdjustment.effective_date.desc(), LeaveAdjustment.id.desc())
    )
    return [svc.adjustment_out(a) for a in rows]


@router.post("/employees/{employee_id}/adjustments", response_model=S.AdjustmentOut, status_code=201)
def add_adjustment(
    employee_id: int,
    body: S.AdjustmentIn,
    p: Principal = Depends(require("leaves.edit")),
    db: Session = Depends(get_db),
) -> S.AdjustmentOut:
    a = svc.add_adjustment(db, p, employees.get(db, p, employee_id), body)
    db.commit()
    db.refresh(a)
    return svc.adjustment_out(a)
