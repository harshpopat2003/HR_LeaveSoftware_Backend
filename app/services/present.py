"""Turns database rows into response bodies."""

from datetime import date

from app import schemas as S
from app.models import AirTicket, Company, DocumentRenewal, Employee, EmployeeDocument, LeaveRequest, User
from app.rules import leave_rules as R
from app.services.rules import RulesCache


def company_ref(c: Company) -> S.CompanyRef:
    return S.CompanyRef(id=c.id, code=c.code, name=c.name)


def document_out(d: EmployeeDocument, today: date, rules: R.RulesConfig) -> S.DocumentOut:
    left, level = R.document_status(d.doc_type, d.expiry_date, today, rules)
    return S.DocumentOut(
        doc_type=d.doc_type,
        label=R.DOC_LABEL[d.doc_type],
        doc_number=d.doc_number,
        expiry_date=d.expiry_date,
        days_left=left,
        level=level,
        level_label=R.DOC_LEVEL_LABEL[level],
    )


def document_row(d: EmployeeDocument, today: date, rules: RulesCache) -> S.DocumentRow:
    e = d.employee
    base = document_out(d, today, rules.get(e.company_id))
    return S.DocumentRow(
        employee_id=e.id,
        emp_no=e.emp_no,
        employee_name=e.full_name,
        company=company_ref(e.company),
        department=e.department.name,
        **base.model_dump(),
    )


def employee_out(e: Employee, today: date, rules: RulesCache) -> S.EmployeeOut:
    r = rules.get(e.company_id)
    order: dict[str, int] = {t: i for i, t in enumerate(R.DOC_TYPES)}
    docs = sorted(e.documents, key=lambda d: order.get(d.doc_type, 9))
    return S.EmployeeOut(
        id=e.id,
        emp_no=e.emp_no,
        first_name=e.first_name,
        last_name=e.last_name,
        full_name=e.full_name,
        gender=e.gender,
        nationality=e.nationality,
        designation=e.designation,
        category=e.category,
        joining_date=e.joining_date,
        ticket_eligible=e.ticket_eligible,
        mobile=e.mobile,
        email=e.email,
        status=e.status,
        leaving_date=e.leaving_date,
        company=company_ref(e.company),
        department=S.DepartmentRef(id=e.department.id, name=e.department.name),
        service=R.service_text(e.joining_date, e.leaving_date or today),
        documents=[document_out(d, today, r) for d in docs],
    )


def leave_out(lv: LeaveRequest) -> S.LeaveOut:
    e = lv.employee
    return S.LeaveOut(
        id=lv.id,
        employee_id=e.id,
        emp_no=e.emp_no,
        employee_name=e.full_name,
        company=company_ref(e.company),
        department=e.department.name,
        leave_type=lv.leave_type.code,
        leave_type_name=lv.leave_type.name,
        start_date=lv.start_date,
        end_date=lv.end_date,
        days=lv.days,
        status=lv.status,
        reason=lv.reason,
        decided_by=lv.decider.full_name if lv.decider else None,
        decided_at=lv.decided_at,
        decision_note=lv.decision_note,
        created_at=lv.created_at,
    )


def leave_brief(lv: R.LeaveFacts | LeaveRequest, days: int | None = None) -> S.LeaveBrief:
    if isinstance(lv, LeaveRequest):
        return S.LeaveBrief(
            id=lv.id,
            leave_type=lv.leave_type.code,
            start_date=lv.start_date,
            end_date=lv.end_date,
            days=lv.days,
            status=lv.status,
        )
    return S.LeaveBrief(
        id=lv.id or 0, leave_type=lv.type_code, start_date=lv.start, end_date=lv.end, days=days or 0, status=lv.status
    )


def balance_out(b: R.Balance) -> S.BalanceOut:
    return S.BalanceOut(
        leave_type=b.type_code,
        entitled=b.entitled,
        used=b.used,
        pending=b.pending,
        adjusted=b.adjusted,
        remaining=b.remaining,
        period=b.period,
        period_start=b.period_start,
        period_end=b.period_end,
    )


def ticket_info_out(t: R.TicketInfo) -> S.TicketInfoOut:
    return S.TicketInfoOut(
        eligible=t.eligible,
        cycle_years=t.cycle_years,
        next_due=t.next_due,
        due_soon=t.due_soon,
        overdue=t.overdue,
        last_issued=t.last_issued,
        issued_count=t.issued_count,
    )


def ticket_out(t: AirTicket, users: dict[int, str], row: R.TicketHistoryRow | None = None) -> S.TicketOut:
    return S.TicketOut(
        id=t.id,
        employee_id=t.employee_id,
        issued_on=t.issued_on,
        destination=t.destination,
        note=t.note,
        issued_by=users.get(t.issued_by) if t.issued_by else None,
        scheduled=row.scheduled if row else None,
        early=row.early if row else False,
        months_early=row.months_early if row else 0,
    )


def renewal_out(r: DocumentRenewal, users: dict[int, str]) -> S.RenewalOut:
    e = r.document.employee
    return S.RenewalOut(
        id=r.id,
        employee_id=e.id,
        emp_no=e.emp_no,
        employee_name=e.full_name,
        doc_type=r.document.doc_type,
        old_number=r.old_number,
        new_number=r.new_number,
        old_expiry=r.old_expiry,
        new_expiry=r.new_expiry,
        renewed_on=r.renewed_on,
        renewed_by=users.get(r.renewed_by) if r.renewed_by else None,
        note=r.note,
    )


def user_out(u: User) -> S.UserOut:
    from app.clock import now_utc

    return S.UserOut(
        id=u.id,
        username=u.username,
        full_name=u.full_name,
        email=u.email,
        role=S.RoleRef(id=u.role.id, key=u.role.key, name=u.role.name),
        is_active=u.is_active,
        last_login_at=u.last_login_at,
        locked=bool(u.locked_until and u.locked_until > now_utc()),
        companies=[company_ref(c) for c in u.companies],
        all_companies=not u.companies,
        created_at=u.created_at,
    )
