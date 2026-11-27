"""Loads what the rules need (leaves, adjustments, tickets) for one or many employees in a few queries."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AirTicket, Employee, LeaveAdjustment, LeaveRequest, LeaveType
from app.rules import leave_rules as R
from app.services.rules import RulesCache


@dataclass
class Bundle:
    leaves: list[R.LeaveFacts] = field(default_factory=list)
    adjustments: list[R.AdjustmentFacts] = field(default_factory=list)
    tickets: list[date] = field(default_factory=list)


def emp_facts(e: Employee) -> R.EmpFacts:
    return R.EmpFacts(e.category, e.joining_date, e.ticket_eligible)


def load(
    db: Session, employee_ids: Iterable[int], *, leaves: bool = True, adjustments: bool = True, tickets: bool = True
) -> dict[int, Bundle]:
    ids = list(set(employee_ids))
    out: dict[int, Bundle] = defaultdict(Bundle)
    if not ids:
        return out
    if leaves:
        rows = db.execute(
            select(
                LeaveRequest.id,
                LeaveRequest.employee_id,
                LeaveType.code,
                LeaveRequest.start_date,
                LeaveRequest.end_date,
                LeaveRequest.status,
            )
            .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
            .where(LeaveRequest.employee_id.in_(ids))
        )
        for lid, eid, code, s, e, st in rows:
            out[eid].leaves.append(R.LeaveFacts(code, s, e, st, lid))
    if adjustments:
        rows2 = db.execute(
            select(LeaveAdjustment.employee_id, LeaveType.code, LeaveAdjustment.days, LeaveAdjustment.effective_date)
            .join(LeaveType, LeaveType.id == LeaveAdjustment.leave_type_id)
            .where(LeaveAdjustment.employee_id.in_(ids))
        )
        for eid, code, d, eff in rows2:
            out[eid].adjustments.append(R.AdjustmentFacts(code, float(d), eff))
    if tickets:
        for eid, issued in db.execute(
            select(AirTicket.employee_id, AirTicket.issued_on).where(AirTicket.employee_id.in_(ids))
        ):
            out[eid].tickets.append(issued)
    return out


def balances(e: Employee, b: Bundle, today: date, rules: RulesCache) -> list[R.Balance]:
    return R.balances(emp_facts(e), b.leaves, today, rules.get(e.company_id), b.adjustments)


def ticket(e: Employee, b: Bundle, today: date, rules: RulesCache) -> R.TicketInfo:
    return R.ticket_info(emp_facts(e), b.tickets, today, rules.get(e.company_id))
