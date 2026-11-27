"""Loads the starting data. Safe to run more than once (existing rows are left alone).

python -m app.seed                      companies, permissions, roles, leave types, rules, admin login
python -m app.seed --demo               the above plus demo logins (password demo123) and demo employees
python -m app.seed --admin-password X   set the admin password (otherwise a random one is printed once)
"""

import argparse
import json
import secrets
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    AirTicket,
    Company,
    Department,
    DocumentRenewal,
    Employee,
    EmployeeDocument,
    LeaveRequest,
    LeaveType,
    Permission,
    Role,
    RolePermission,
    RuleSet,
    User,
)
from app.rules.leave_rules import DEFAULT_RULES, leave_days
from app.security import hash_password

COMPANIES = [("SPL", "Sabin Plastic"), ("SSG", "Sabin Signs"), ("TRD", "Trading")]

PERMISSIONS = [
    ("dashboard.view", "View dashboard", "Dashboard"),
    ("employees.view", "View employees and search", "Employees"),
    ("employees.edit", "Add and edit employees", "Employees"),
    ("leaves.view", "View leaves", "Leave"),
    ("leaves.edit", "Add and edit leaves", "Leave"),
    ("leaves.approve", "Approve or reject leave", "Leave"),
    ("users.manage", "Manage user logins", "Administration"),
    ("roles.manage", "Create roles and set permissions", "Administration"),
    ("rules.edit", "Edit leave and ticket rules", "Administration"),
    ("data.export", "Export data to Excel", "Reports"),
    ("documents.view", "View passport, Emirates ID and visa expiry", "Documents"),
    ("documents.edit", "Update and renew passports, Emirates IDs and visas", "Documents"),
]

ROLES: list[tuple[str, str, str, list[str]]] = [
    (
        "administrator",
        "System Administrator",
        "Full access. Manages logins, roles and permissions, and leave rules.",
        [p[0] for p in PERMISSIONS],
    ),
    (
        "manager",
        "Manager",
        "Views data and dashboards, approves or rejects leave, sees document expiry.",
        ["dashboard.view", "employees.view", "leaves.view", "leaves.approve", "data.export", "documents.view"],
    ),
    (
        "hr",
        "HR",
        "Enters and edits employees and leaves. Tracks document renewals.",
        [
            "dashboard.view",
            "employees.view",
            "employees.edit",
            "leaves.view",
            "leaves.edit",
            "data.export",
            "documents.view",
            "documents.edit",
        ],
    ),
]

LEAVE_TYPES = [("annual", "Annual leave"), ("emergency", "Emergency leave"), ("other", "Other leave")]

DEMO_USERS = [
    ("manager", "Operations Manager", "manager@example.com", "manager"),
    ("hr1", "HR Officer 1", "hr1@example.com", "hr"),
    ("hr2", "HR Officer 2", "hr2@example.com", "hr"),
]
DEMO_PASSWORD = "demo123"  # noqa: S105 - demo logins only, never used with --demo off

DEMO_FILE = Path(__file__).parent / "seed_data" / "demo.json"


def _get_or_add(db: Session, model: Any, lookup: dict[str, Any], **values: Any) -> Any:
    row = db.scalar(select(model).filter_by(**lookup))
    if row is None:
        row = model(**lookup, **values)
        db.add(row)
        db.flush()
    return row


def seed_base(db: Session, admin_password: str | None) -> str | None:
    """Returns the generated admin password when one was made up."""
    for code, name in COMPANIES:
        _get_or_add(db, Company, {"code": code}, name=name)
    perms = {
        key: _get_or_add(db, Permission, {"key": key}, label=label, group_name=group)
        for key, label, group in PERMISSIONS
    }
    for key, name, desc, keys in ROLES:
        role = _get_or_add(db, Role, {"key": key}, name=name, description=desc, is_system=True)
        have = set(db.scalars(select(RolePermission.permission_id).where(RolePermission.role_id == role.id)))
        for k in keys:
            if perms[k].id not in have:
                db.add(RolePermission(role_id=role.id, permission_id=perms[k].id))
    for code, name in LEAVE_TYPES:
        _get_or_add(db, LeaveType, {"code": code}, name=name)
    if db.scalar(select(RuleSet.id).where(RuleSet.company_id.is_(None))) is None:
        db.add(RuleSet(company_id=None, config=DEFAULT_RULES.model_dump(mode="json"), version=1))
    db.flush()
    generated = None
    if db.scalar(select(User.id).where(User.username == "admin")) is None:
        if not admin_password:
            admin_password = generated = secrets.token_urlsafe(12)
        admin_role = db.scalar(select(Role).where(Role.key == "administrator"))
        assert admin_role is not None  # noqa: S101
        db.add(
            User(
                username="admin",
                full_name="System Administrator",
                email="admin@example.com",
                password_hash=hash_password(admin_password),
                role_id=admin_role.id,
            )
        )
    elif admin_password:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin is not None  # noqa: S101
        admin.password_hash = hash_password(admin_password)
    db.flush()
    return generated


def _d(s: str) -> date | None:
    return date.fromisoformat(s) if s else None


def seed_demo(db: Session) -> None:
    roles = {r.key: r for r in db.scalars(select(Role))}
    for username, full, email, role in DEMO_USERS:
        _get_or_add(
            db,
            User,
            {"username": username},
            full_name=full,
            email=email,
            password_hash=hash_password(DEMO_PASSWORD),
            role_id=roles[role].id,
        )
    admin = db.scalar(select(User).where(User.username == "admin"))
    if admin is not None and db.scalar(select(Employee.id).limit(1)) is None:
        admin.password_hash = hash_password(DEMO_PASSWORD)
    db.flush()
    if db.scalar(select(Employee.id).limit(1)) is not None:
        return  # demo employees already loaded (or real data exists): do not mix
    data = json.loads(DEMO_FILE.read_text(encoding="utf-8"))
    users = {u.full_name: u.id for u in db.scalars(select(User))}
    companies = {c.name: c for c in db.scalars(select(Company))}
    types = {t.code: t for t in db.scalars(select(LeaveType))}
    by_no: dict[str, Employee] = {}
    for x in data["employees"]:
        comp = companies[x["company"]]
        dept = _get_or_add(db, Department, {"company_id": comp.id, "name": x["department"]})
        e = Employee(
            company_id=comp.id,
            department_id=dept.id,
            emp_no=x["empNo"],
            first_name=x["firstName"],
            last_name=x["lastName"],
            gender=x["gender"],
            nationality=x["nationality"],
            designation=x["designation"],
            category=x["category"],
            joining_date=date.fromisoformat(x["joiningDate"]),
            ticket_eligible=x["ticketEligible"],
            mobile=x["mobile"],
            email=x["email"],
            status=x["status"].lower(),
        )
        for doc_type, num, exp in (
            ("passport", "passportNumber", "passportExpiry"),
            ("emirates_id", "emiratesIdNumber", "emiratesIdExpiry"),
            ("visit_visa", "visitVisaNumber", "visitVisaExpiry"),
        ):
            expiry = _d(x.get(exp, ""))
            if expiry:
                e.documents.append(EmployeeDocument(doc_type=doc_type, doc_number=x.get(num, ""), expiry_date=expiry))
        db.add(e)
        by_no[e.emp_no] = e
    db.flush()
    hr1 = users.get("HR Officer 1")
    manager = users.get("Operations Manager")
    for lv in data["leaves"]:
        e = by_no[lv["empNo"]]
        start, end = date.fromisoformat(lv["from"]), date.fromisoformat(lv["to"])
        decided = lv["status"] in ("approved", "rejected")
        db.add(
            LeaveRequest(
                employee_id=e.id,
                leave_type_id=types[lv["type"]].id,
                start_date=start,
                end_date=end,
                days=leave_days(start, end),
                status=lv["status"],
                reason=lv.get("reason", ""),
                requested_by=hr1,
                decided_by=manager if decided else None,
                decision_note="Imported from the HR sheet" if lv.get("source") == "excel" else "",
            )
        )
    for r in data["renewals"]:
        e = by_no[r["empNo"]]
        doc = next(d for d in e.documents if d.doc_type == r["doc"])
        db.add(
            DocumentRenewal(
                employee_document_id=doc.id,
                old_number=doc.doc_number,
                new_number=r["newNumber"] or doc.doc_number,
                old_expiry=date.fromisoformat(r["oldExpiry"]),
                new_expiry=date.fromisoformat(r["newExpiry"]),
                renewed_on=date.fromisoformat(r["renewedOn"]),
                renewed_by=users.get(r["by"]),
                note=r["note"],
            )
        )
    for t in data["tickets"]:
        e = by_no[t["empNo"]]
        db.add(
            AirTicket(
                employee_id=e.id,
                issued_on=date.fromisoformat(t["issuedOn"]),
                destination=t["destination"],
                note=t["note"],
                issued_by=users.get(t["by"]),
            )
        )
    db.flush()


def run(db: Session, demo: bool, admin_password: str | None = None) -> str | None:
    generated = seed_base(db, admin_password)
    if demo:
        seed_demo(db)
        generated = None
    db.commit()
    return generated


def main() -> None:
    ap = argparse.ArgumentParser(description="Load starting data into the HR database.")
    ap.add_argument("--demo", action="store_true", help="also load demo logins and demo employees")
    ap.add_argument("--admin-password", help="password for the 'admin' login")
    args = ap.parse_args()
    with SessionLocal() as db:
        generated = run(db, args.demo, args.admin_password)
    print("Seed done.")  # noqa: T201
    if args.demo:
        print("Demo logins: admin, manager, hr1, hr2 (password demo123).")  # noqa: T201
    elif generated:
        print(f"Admin login created. Username: admin  Password: {generated}  (shown once, change it after signing in)")  # noqa: T201


if __name__ == "__main__":
    main()
