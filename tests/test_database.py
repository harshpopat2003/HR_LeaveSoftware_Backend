"""Rules the database enforces by itself, and that the migrations match the models."""

from datetime import date

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.db import Base
from app.models import AuditLog, Department, DocumentRenewal, Employee, EmployeeDocument
from tests.conftest import company_id


@pytest.mark.filterwarnings("ignore:Expression #1")  # alembic cannot compare the trigram index expression
def test_migrations_match_models(db: Session) -> None:
    ctx = MigrationContext.configure(db.connection(), opts={"compare_type": True})
    assert compare_metadata(ctx, Base.metadata) == []


def test_extensions_and_constraints_exist(db: Session) -> None:
    ext = set(db.scalars(text("SELECT extname FROM pg_extension")))
    assert {"btree_gist", "pg_trgm", "citext"} <= ext
    names = set(db.scalars(text("SELECT conname FROM pg_constraint")))
    assert {"ex_leave_requests_no_overlap", "fk_employees_department_same_company", "ck_employees_category"} <= names


def test_audit_log_is_append_only(db: Session) -> None:
    db.add(AuditLog(action="test", entity="x"))
    db.flush()
    with pytest.raises(DBAPIError, match="append only"):
        db.execute(text("UPDATE audit_log SET action = 'changed'"))
    db.rollback()
    db.add(AuditLog(action="test", entity="x"))
    db.flush()
    with pytest.raises(DBAPIError, match="append only"):
        db.execute(text("DELETE FROM audit_log"))
    db.rollback()
    with pytest.raises(DBAPIError, match="append only"):
        db.execute(text("TRUNCATE audit_log"))
    db.rollback()


def test_department_must_belong_to_same_company(db: Session, emp) -> None:
    e = emp("SPW013")
    other = db.scalar(select(Department.id).where(Department.company_id == company_id(db, "SSG")).limit(1))
    with pytest.raises(IntegrityError, match="fk_employees_department_same_company"):
        db.execute(text("UPDATE employees SET department_id = :d WHERE id = :e"), {"d": other, "e": e.id})
    db.rollback()


@pytest.mark.parametrize(
    ("sql", "constraint"),
    [
        ("UPDATE employees SET category = 'C'", "ck_employees_category"),
        ("UPDATE employees SET status = 'gone'", "ck_employees_status"),
        ("UPDATE employees SET leaving_date = '1990-01-01'", "ck_employees_leaving_after_joining"),
        ("UPDATE leave_requests SET end_date = start_date - 1", "ck_leave_requests_dates"),
        ("UPDATE leave_requests SET status = 'maybe'", "ck_leave_requests_status"),
        ("UPDATE employee_documents SET doc_type = 'licence'", "ck_employee_documents_type"),
        ("UPDATE leave_adjustments SET reason = ' '", None),
    ],
)
def test_check_constraints(db: Session, sql: str, constraint: str | None) -> None:
    if "leave_adjustments" in sql:
        db.execute(
            text(
                "INSERT INTO leave_adjustments (employee_id, leave_type_id, days, effective_date, reason) "
                "SELECT min(e.id), min(t.id), 1, '2026-01-01', 'ok' FROM employees e, leave_types t"
            )
        )
        constraint = "ck_leave_adjustments_reason"
    with pytest.raises(IntegrityError, match=constraint):
        db.execute(text(sql))
    db.rollback()


def test_renewal_must_extend_and_document_type_unique(db: Session, emp) -> None:
    e = emp("SPW013")
    doc = next(d for d in e.documents if d.doc_type == "passport")
    db.add(
        DocumentRenewal(
            employee_document_id=doc.id,
            old_expiry=date(2030, 1, 1),
            new_expiry=date(2029, 1, 1),
            renewed_on=date(2026, 1, 1),
        )
    )
    with pytest.raises(IntegrityError, match="ck_document_renewals_later"):
        db.flush()
    db.rollback()
    db.add(EmployeeDocument(employee_id=e.id, doc_type="passport", expiry_date=date(2031, 1, 1)))
    with pytest.raises(IntegrityError, match="uq_employee_documents_type"):
        db.flush()
    db.rollback()


def test_emp_no_unique_across_group(db: Session, emp) -> None:
    e = emp("SPW013")
    other = emp("SPE158")
    with pytest.raises(IntegrityError):
        db.execute(text("UPDATE employees SET emp_no = :n WHERE id = :i"), {"n": e.emp_no, "i": other.id})
    db.rollback()


def test_employee_with_leaves_cannot_be_deleted(db: Session, emp) -> None:
    e = emp("SPW013")
    with pytest.raises(IntegrityError):
        db.execute(text("DELETE FROM employees WHERE id = :i"), {"i": e.id})
    db.rollback()
    assert db.get(Employee, e.id) is not None


def test_health(client: TestClient) -> None:
    assert client.get("/api/v1/health").json()["status"] == "ok"
    r = client.get("/api/v1/health/ready").json()
    assert r["status"] == "ready" and r["migration"] == "0001"


def test_security_headers_and_cors(client: TestClient) -> None:
    r = client.get("/api/v1/health")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["cache-control"] == "no-store"
    pre = client.options(
        "/api/v1/auth/login", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST"}
    )
    assert pre.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert pre.headers.get("access-control-allow-credentials") == "true"
    evil = client.options(
        "/api/v1/auth/login", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"}
    )
    assert "access-control-allow-origin" not in evil.headers


def test_unknown_route_uses_error_shape(client: TestClient) -> None:
    r = client.get("/api/v1/nothing-here")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"


def test_seed_is_idempotent(db: Session) -> None:
    from app import seed
    from app.models import Company, Permission, Role, User

    before = [db.scalar(select(text("count(*)")).select_from(m)) for m in (Company, Permission, Role, User, Employee)]
    seed.run(db, demo=True)
    after = [db.scalar(select(text("count(*)")).select_from(m)) for m in (Company, Permission, Role, User, Employee)]
    assert before == after


def test_production_refuses_dev_secret() -> None:
    from app.config import DEV_SECRET, Settings

    with pytest.raises(RuntimeError):
        Settings(cookie_secure=True, jwt_secret=DEV_SECRET).check_production()
    with pytest.raises(RuntimeError):
        Settings(cookie_secure=True, jwt_secret="short").check_production()
    Settings(cookie_secure=True, jwt_secret="x" * 40).check_production()
    Settings(cookie_secure=False).check_production()
