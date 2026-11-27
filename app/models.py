"""Database tables. The design is in 'HR backend: database design for approval'.

Rules kept by the database itself:
- check constraints on every closed set (category, status, document type),
- the department of an employee belongs to the same company (composite foreign key),
- no two pending/approved leaves of one employee overlap (exclusion constraint, created in the migration),
- audit_log is append only (trigger, created in the migration).
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

DOC_TYPES = ("passport", "emirates_id", "visit_visa")
LEAVE_STATUSES = ("pending", "approved", "rejected", "cancelled")
EMPLOYEE_STATUSES = ("active", "inactive", "left")


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Timestamps:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Tracked(Timestamps):
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    updated_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))


# ---------------------------------------------------------------- companies and departments
class Company(Tracked, Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class Department(Tracked, Base):
    __tablename__ = "departments"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_departments_company_name"),
        UniqueConstraint("id", "company_id", name="uq_departments_id_company"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    company: Mapped[Company] = relationship(lazy="joined")


# ---------------------------------------------------------------- employees and documents
class Employee(Tracked, Base):
    __tablename__ = "employees"
    __table_args__ = (
        ForeignKeyConstraint(
            ["department_id", "company_id"],
            ["departments.id", "departments.company_id"],
            name="fk_employees_department_same_company",
            ondelete="RESTRICT",
        ),
        CheckConstraint("category IN ('A', 'B')", name="ck_employees_category"),
        CheckConstraint(_in("status", EMPLOYEE_STATUSES), name="ck_employees_status"),
        CheckConstraint(
            "leaving_date IS NULL OR leaving_date >= joining_date", name="ck_employees_leaving_after_joining"
        ),
        Index("ix_employees_company_status", "company_id", "status"),
        Index(
            "ix_employees_name_trgm",
            text("(lower(first_name || ' ' || last_name)) gin_trgm_ops"),
            postgresql_using="gin",
        ),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id", ondelete="RESTRICT"), nullable=False)
    department_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    emp_no: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    gender: Mapped[str] = mapped_column(String(10), nullable=False, server_default="")
    nationality: Mapped[str] = mapped_column(String(60), nullable=False, server_default="")
    designation: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    category: Mapped[str] = mapped_column(String(1), nullable=False)
    joining_date: Mapped[date] = mapped_column(Date, nullable=False)
    ticket_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    mobile: Mapped[str] = mapped_column(String(30), nullable=False, server_default="")
    email: Mapped[str] = mapped_column(String(200), nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="active")
    leaving_date: Mapped[date | None] = mapped_column(Date)

    company: Mapped[Company] = relationship(lazy="joined")
    department: Mapped[Department] = relationship(
        lazy="joined",
        primaryjoin="and_(Employee.department_id == Department.id, Employee.company_id == Department.company_id)",
        foreign_keys="[Employee.department_id, Employee.company_id]",
        viewonly=True,
    )
    documents: Mapped[list["EmployeeDocument"]] = relationship(
        back_populates="employee", lazy="selectin", cascade="all, delete-orphan"
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class EmployeeDocument(Tracked, Base):
    __tablename__ = "employee_documents"
    __table_args__ = (
        UniqueConstraint("employee_id", "doc_type", name="uq_employee_documents_type"),
        CheckConstraint(_in("doc_type", DOC_TYPES), name="ck_employee_documents_type"),
        Index("ix_employee_documents_expiry", "expiry_date"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    employee_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(20), nullable=False)
    doc_number: Mapped[str] = mapped_column(String(50), nullable=False, server_default="")
    expiry_date: Mapped[date] = mapped_column(Date, nullable=False)
    employee: Mapped[Employee] = relationship(back_populates="documents")


class DocumentRenewal(Base):
    __tablename__ = "document_renewals"
    __table_args__ = (CheckConstraint("new_expiry > old_expiry", name="ck_document_renewals_later"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    employee_document_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("employee_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_number: Mapped[str] = mapped_column(String(50), nullable=False, server_default="")
    new_number: Mapped[str] = mapped_column(String(50), nullable=False, server_default="")
    old_expiry: Mapped[date] = mapped_column(Date, nullable=False)
    new_expiry: Mapped[date] = mapped_column(Date, nullable=False)
    renewed_on: Mapped[date] = mapped_column(Date, nullable=False)
    renewed_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    document: Mapped[EmployeeDocument] = relationship(lazy="joined")


# ---------------------------------------------------------------- leave
class LeaveType(Tracked, Base):
    __tablename__ = "leave_types"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class LeaveRequest(Tracked, Base):
    __tablename__ = "leave_requests"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_leave_requests_dates"),
        CheckConstraint("days >= 0", name="ck_leave_requests_days"),
        CheckConstraint(_in("status", LEAVE_STATUSES), name="ck_leave_requests_status"),
        Index("ix_leave_requests_employee_start", "employee_id", "start_date"),
        Index("ix_leave_requests_status", "status"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    leave_type_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leave_types.id", ondelete="RESTRICT"), nullable=False
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    days: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="pending")
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    requested_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    decided_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    employee: Mapped[Employee] = relationship(lazy="joined")
    leave_type: Mapped[LeaveType] = relationship(lazy="joined")
    decider: Mapped["User | None"] = relationship(foreign_keys=[decided_by], lazy="joined")


class LeaveAdjustment(Tracked, Base):
    __tablename__ = "leave_adjustments"
    __table_args__ = (CheckConstraint("length(trim(reason)) > 0", name="ck_leave_adjustments_reason"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    leave_type_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("leave_types.id", ondelete="RESTRICT"), nullable=False
    )
    days: Mapped[Decimal] = mapped_column(Numeric(6, 1), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    leave_type: Mapped[LeaveType] = relationship(lazy="joined")


# ---------------------------------------------------------------- air tickets
class AirTicket(Tracked, Base):
    __tablename__ = "air_tickets"
    __table_args__ = (Index("ix_air_tickets_employee_issued", "employee_id", "issued_on"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False
    )
    issued_on: Mapped[date] = mapped_column(Date, nullable=False)
    destination: Mapped[str] = mapped_column(String(100), nullable=False, server_default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    issued_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))


# ---------------------------------------------------------------- users and permissions
class Role(Timestamps, Base):
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    permissions: Mapped[list["Permission"]] = relationship(
        secondary="role_permissions", lazy="selectin", order_by="Permission.id"
    )


class Permission(Base):
    __tablename__ = "permissions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    key: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    group_name: Mapped[str] = mapped_column(String(60), nullable=False)


class RolePermission(Base):
    __tablename__ = "role_permissions"
    role_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True)
    permission_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class User(Timestamps, Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    email: Mapped[str | None] = mapped_column(CITEXT, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("roles.id", ondelete="RESTRICT"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_logins: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    role: Mapped[Role] = relationship(lazy="joined")
    companies: Mapped[list[Company]] = relationship(
        secondary="user_company_access", lazy="selectin", order_by="Company.id"
    )


class UserCompanyAccess(Base):
    __tablename__ = "user_company_access"
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    company_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("companies.id", ondelete="CASCADE"), primary_key=True
    )


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str] = mapped_column(String(300), nullable=False, server_default="")


# ---------------------------------------------------------------- rules, audit, reminders
class RuleSet(Base):
    __tablename__ = "rule_sets"
    __table_args__ = (Index("uq_rule_sets_company_version", text("coalesce(company_id, 0)"), "version", unique=True),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    company_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("companies.id", ondelete="CASCADE"))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"))


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_log_entity", "entity", "entity_id"), Index("ix_audit_log_at", "at"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    entity: Mapped[str] = mapped_column(String(40), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(BigInteger)
    company_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("companies.id", ondelete="SET NULL"))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, server_default="")


class ReminderLog(Base):
    __tablename__ = "reminder_log"
    __table_args__ = (UniqueConstraint("user_id", "kind", "channel", "sent_on", name="uq_reminder_log_once_a_day"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False, server_default="in_app")
    sent_on: Mapped[date] = mapped_column(Date, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
