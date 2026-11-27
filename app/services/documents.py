"""Passports, Emirates IDs and visit visas: corrections, renewals, alerts and the daily reminder."""

from collections import Counter
from datetime import date

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, joinedload

from app import schemas as S
from app.clock import today
from app.deps import Principal
from app.errors import invalid, not_found
from app.models import DocumentRenewal, Employee, EmployeeDocument, ReminderLog
from app.rules import leave_rules as R
from app.services import audit, present
from app.services.rules import RulesCache

ALERT_LEVELS = ("expired", "urgent", "soon")


def find(e: Employee, doc_type: str) -> EmployeeDocument | None:
    return next((d for d in e.documents if d.doc_type == doc_type), None)


def put(db: Session, p: Principal, e: Employee, doc_type: str, body: S.DocumentPut) -> EmployeeDocument | None:
    """Correct a document's number or expiry (not a renewal). An empty expiry removes the record."""
    doc = find(e, doc_type)
    if body.expiry_date is None:
        if doc is None:
            return None
        audit.record(db, p.id, "delete", "employee_document", doc.id, e.company_id, before=audit.snap(doc))
        e.documents.remove(doc)
        db.flush()
        return None
    if doc is None:
        doc = EmployeeDocument(
            doc_type=doc_type,
            doc_number=body.doc_number,
            expiry_date=body.expiry_date,
            created_by=p.id,
            updated_by=p.id,
        )
        e.documents.append(doc)
        db.flush()
        audit.record(db, p.id, "create", "employee_document", doc.id, e.company_id, after=audit.snap(doc))
        return doc
    before = audit.snap(doc)
    doc.doc_number = body.doc_number
    doc.expiry_date = body.expiry_date
    doc.updated_by = p.id
    db.flush()
    audit.record(db, p.id, "update", "employee_document", doc.id, e.company_id, before=before, after=audit.snap(doc))
    return doc


def renew(
    db: Session, p: Principal, e: Employee, doc_type: str, body: S.DocumentRenewIn
) -> tuple[EmployeeDocument, DocumentRenewal]:
    doc = find(e, doc_type)
    if doc is None:
        raise not_found(f"{R.DOC_LABEL[doc_type]} record")
    if body.new_expiry <= doc.expiry_date:
        raise invalid("The new expiry date must be later than the current one.", {"field": "newExpiry"})
    renewed_on = body.renewed_on or today()
    if renewed_on > today():
        raise invalid("The renewal date cannot be in the future.", {"field": "renewedOn"})
    ren = DocumentRenewal(
        employee_document_id=doc.id,
        old_number=doc.doc_number,
        new_number=body.new_number or doc.doc_number,
        old_expiry=doc.expiry_date,
        new_expiry=body.new_expiry,
        renewed_on=renewed_on,
        renewed_by=p.id,
        note=body.note,
    )
    before = audit.snap(doc)
    doc.expiry_date = body.new_expiry
    if body.new_number:
        doc.doc_number = body.new_number
    doc.updated_by = p.id
    db.add(ren)
    db.flush()
    audit.record(db, p.id, "renew", "employee_document", doc.id, e.company_id, before=before, after=audit.snap(doc))
    return doc, ren


def _stmt(companies: list[int] | None, doc_type: str | None = None) -> Select[EmployeeDocument]:
    stmt = (
        select(EmployeeDocument)
        .join(Employee, Employee.id == EmployeeDocument.employee_id)
        .where(Employee.status == "active")
        .options(joinedload(EmployeeDocument.employee))
    )
    if companies is not None:
        stmt = stmt.where(Employee.company_id.in_(companies))
    if doc_type:
        stmt = stmt.where(EmployeeDocument.doc_type == doc_type)
    return stmt.order_by(EmployeeDocument.expiry_date, Employee.emp_no)


def rows(
    db: Session, companies: list[int] | None, doc_type: str | None = None, levels: tuple[str, ...] | None = None
) -> list[S.DocumentRow]:
    t = today()
    rules = RulesCache(db)
    out = [present.document_row(d, t, rules) for d in db.scalars(_stmt(companies, doc_type)).unique()]
    if levels:
        out = [r for r in out if r.level in levels]
    return out


def summary(db: Session, companies: list[int] | None) -> list[S.DocumentsSummaryRow]:
    t = today()
    rules = RulesCache(db)
    counts: dict[str, Counter[str]] = {dt: Counter() for dt in R.DOC_TYPES}
    for d in db.scalars(_stmt(companies)).unique():
        _, level = R.document_status(d.doc_type, d.expiry_date, t, rules.get(d.employee.company_id))
        counts[d.doc_type][level] += 1
    emp_stmt = select(Employee).where(Employee.status == "active")
    if companies is not None:
        emp_stmt = emp_stmt.where(Employee.company_id.in_(companies))
    active = list(db.scalars(emp_stmt).unique())
    out = []
    for dt in R.DOC_TYPES:
        c = counts[dt]
        # A missing visit visa is normal (most staff hold residence visas),
        # so only passports and Emirates IDs count as missing.
        missing = 0 if dt == "visit_visa" else sum(1 for e in active if find(e, dt) is None)
        out.append(
            S.DocumentsSummaryRow(
                doc_type=dt,
                label=R.DOC_LABEL[dt],
                expired=c["expired"],
                urgent=c["urgent"],
                soon=c["soon"],
                ok=c["ok"],
                missing=missing,
            )
        )
    return out


def renewals(db: Session, companies: list[int] | None, employee_id: int | None) -> Select[DocumentRenewal]:
    stmt = (
        select(DocumentRenewal)
        .join(EmployeeDocument, EmployeeDocument.id == DocumentRenewal.employee_document_id)
        .join(Employee, Employee.id == EmployeeDocument.employee_id)
    )
    if companies is not None:
        stmt = stmt.where(Employee.company_id.in_(companies))
    if employee_id is not None:
        stmt = stmt.where(Employee.id == employee_id)
    return stmt.order_by(DocumentRenewal.renewed_on.desc(), DocumentRenewal.id.desc())


REMINDER_KIND = "daily_expiry"


def reminder_seen(db: Session, user_id: int, on: date) -> bool:
    return (
        db.scalar(
            select(ReminderLog.id).where(
                ReminderLog.user_id == user_id,
                ReminderLog.kind == REMINDER_KIND,
                ReminderLog.channel == "in_app",
                ReminderLog.sent_on == on,
            )
        )
        is not None
    )


def mark_seen(db: Session, user_id: int, on: date) -> None:
    db.execute(
        insert(ReminderLog)
        .values(user_id=user_id, kind=REMINDER_KIND, channel="in_app", sent_on=on)
        .on_conflict_do_nothing(constraint="uq_reminder_log_once_a_day")
    )
