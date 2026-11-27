"""Request and response bodies. JSON uses camelCase (like the frontend); Python uses snake_case."""

from datetime import date, datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from pydantic.alias_generators import to_camel

from app.rules.leave_rules import RulesConfig

T = TypeVar("T")

DocTypeIn = Literal["passport", "emirates_id", "visit_visa"]
Category = Literal["A", "B"]
EmployeeStatus = Literal["active", "inactive", "left"]
LeaveStatus = Literal["pending", "approved", "rejected", "cancelled"]


class Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


class CamelIn(Camel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", str_strip_whitespace=True
    )


class Page(Camel, Generic[T]):
    items: list[T]
    total: int
    page: int
    size: int


# ---------------------------------------------------------------- auth
class LoginIn(CamelIn):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=200)


class ChangePasswordIn(CamelIn):
    current_password: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


class RoleRef(Camel):
    id: int
    key: str
    name: str


class CompanyOut(Camel):
    id: int
    code: str
    name: str
    is_active: bool


class MeOut(Camel):
    id: int
    username: str
    full_name: str
    email: str | None
    role: RoleRef
    permissions: list[str]
    companies: list[CompanyOut]
    all_companies: bool


class TokenOut(Camel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: MeOut


# ---------------------------------------------------------------- companies and departments
class CompanyRef(Camel):
    id: int
    code: str
    name: str


class DepartmentOut(Camel):
    id: int
    company_id: int
    name: str
    is_active: bool


class DepartmentIn(CamelIn):
    company_id: int
    name: str = Field(min_length=1, max_length=100)


class DepartmentPatch(CamelIn):
    name: str | None = Field(None, min_length=1, max_length=100)
    is_active: bool | None = None


class DepartmentRef(Camel):
    id: int
    name: str


# ---------------------------------------------------------------- documents
class DocumentOut(Camel):
    doc_type: str
    label: str
    doc_number: str
    expiry_date: date
    days_left: int | None
    level: str
    level_label: str


class DocumentPut(CamelIn):
    doc_number: str = Field("", max_length=50)
    expiry_date: date | None = None  # None removes the record (e.g. no longer on a visit visa)


class DocumentRenewIn(CamelIn):
    new_expiry: date
    new_number: str = Field("", max_length=50)
    renewed_on: date | None = None
    note: str = Field("", max_length=1000)


class RenewalOut(Camel):
    id: int
    employee_id: int
    emp_no: str
    employee_name: str
    doc_type: str
    old_number: str
    new_number: str
    old_expiry: date
    new_expiry: date
    renewed_on: date
    renewed_by: str | None
    note: str


class DocumentRow(Camel):
    employee_id: int
    emp_no: str
    employee_name: str
    company: CompanyRef
    department: str
    doc_type: str
    label: str
    doc_number: str
    expiry_date: date
    days_left: int | None
    level: str
    level_label: str


class DocumentsSummaryRow(Camel):
    doc_type: str
    label: str
    expired: int
    urgent: int
    soon: int
    ok: int
    missing: int


class ReminderOut(Camel):
    date: date
    seen: bool
    documents: list[DocumentRow]
    tickets: list["TicketDueRow"]


# ---------------------------------------------------------------- employees
class EmployeeBase(CamelIn):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field("", max_length=100)
    gender: str = Field("", max_length=10)
    nationality: str = Field("", max_length=60)
    designation: str = Field("", max_length=100)
    category: Category
    joining_date: date
    ticket_eligible: bool = False
    mobile: str = Field("", max_length=30)
    email: str = Field("", max_length=200)
    status: EmployeeStatus = "active"
    leaving_date: date | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        if v and ("@" not in v or " " in v):
            raise ValueError("not a valid email address")
        return v


class DocumentInline(CamelIn):
    doc_type: DocTypeIn
    doc_number: str = Field("", max_length=50)
    expiry_date: date


class EmployeeCreate(EmployeeBase):
    company_id: int
    department_id: int
    emp_no: str = Field(min_length=1, max_length=20, pattern=r"^[A-Za-z0-9\-/]+$")
    documents: list[DocumentInline] = []


class EmployeePatch(CamelIn):
    company_id: int | None = None
    department_id: int | None = None
    emp_no: str | None = Field(None, min_length=1, max_length=20, pattern=r"^[A-Za-z0-9\-/]+$")
    first_name: str | None = Field(None, min_length=1, max_length=100)
    last_name: str | None = Field(None, max_length=100)
    gender: str | None = Field(None, max_length=10)
    nationality: str | None = Field(None, max_length=60)
    designation: str | None = Field(None, max_length=100)
    category: Category | None = None
    joining_date: date | None = None
    ticket_eligible: bool | None = None
    mobile: str | None = Field(None, max_length=30)
    email: str | None = Field(None, max_length=200)
    status: EmployeeStatus | None = None
    leaving_date: date | None = None


class EmployeeOut(Camel):
    id: int
    emp_no: str
    first_name: str
    last_name: str
    full_name: str
    gender: str
    nationality: str
    designation: str
    category: str
    joining_date: date
    ticket_eligible: bool
    mobile: str
    email: str
    status: str
    leaving_date: date | None
    company: CompanyRef
    department: DepartmentRef
    service: str
    documents: list[DocumentOut]


class BalanceOut(Camel):
    leave_type: str
    entitled: float
    used: int
    pending: int
    adjusted: float
    remaining: float
    period: str
    period_start: date
    period_end: date


class TicketInfoOut(Camel):
    eligible: bool
    cycle_years: int
    next_due: date | None
    due_soon: bool
    overdue: bool
    last_issued: date | None
    issued_count: int


class LeaveBrief(Camel):
    id: int
    leave_type: str
    start_date: date
    end_date: date
    days: int
    status: str


class EmployeeSummary(Camel):
    employee: EmployeeOut
    balances: list[BalanceOut]
    ticket: TicketInfoOut
    last_leave: LeaveBrief | None
    next_leave: LeaveBrief | None
    next_leave_plan: date | None


class SearchHit(Camel):
    id: int
    emp_no: str
    full_name: str
    company: CompanyRef
    department: str
    designation: str
    category: str
    status: str
    last_leave: LeaveBrief | None
    annual_remaining: float | None
    ticket: TicketInfoOut


class YearStat(Camel):
    year: int
    annual: int
    emergency: int
    other: int


class MonthStat(Camel):
    month: str  # YYYY-MM
    days: int


class EmployeeStats(Camel):
    by_year: list[YearStat]
    last_12_months: list[MonthStat]
    by_type: dict[str, int]
    by_status: dict[str, int]


# ---------------------------------------------------------------- leave types, leaves, adjustments
class LeaveTypeOut(Camel):
    id: int
    code: str
    name: str
    is_active: bool


class LeaveTypeIn(CamelIn):
    code: str = Field(min_length=1, max_length=30, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=60)


class LeaveTypePatch(CamelIn):
    name: str | None = Field(None, min_length=1, max_length=60)
    is_active: bool | None = None


class LeaveIn(CamelIn):
    employee_id: int
    leave_type: str = Field(min_length=1, max_length=30)
    start_date: date
    end_date: date
    reason: str = Field("", max_length=2000)


class LeavePreviewIn(LeaveIn):
    exclude_id: int | None = None


class LeavePatch(CamelIn):
    leave_type: str | None = Field(None, min_length=1, max_length=30)
    start_date: date | None = None
    end_date: date | None = None
    reason: str | None = Field(None, max_length=2000)


class DecisionIn(CamelIn):
    note: str = Field("", max_length=2000)


class RejectIn(CamelIn):
    note: str = Field(min_length=1, max_length=2000)


class ValidationOut(Camel):
    ok: bool
    errors: list[str]
    warnings: list[str]
    days: int


class LeaveOut(Camel):
    id: int
    employee_id: int
    emp_no: str
    employee_name: str
    company: CompanyRef
    department: str
    leave_type: str
    leave_type_name: str
    start_date: date
    end_date: date
    days: int
    status: str
    reason: str
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str
    created_at: datetime


class LeaveSaved(Camel):
    leave: LeaveOut
    warnings: list[str]


class ApprovalRow(Camel):
    leave: LeaveOut
    balance: BalanceOut | None


class AdjustmentIn(CamelIn):
    leave_type: str = Field(min_length=1, max_length=30)
    days: float = Field(ge=-730, le=730)
    effective_date: date
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("days")
    @classmethod
    def _half_days(cls, v: float) -> float:
        if v == 0 or round(v * 2) != v * 2:
            raise ValueError("must be a non-zero number of whole or half days")
        return v


class AdjustmentOut(Camel):
    id: int
    employee_id: int
    leave_type: str
    days: float
    effective_date: date
    reason: str
    created_at: datetime


# ---------------------------------------------------------------- tickets
class TicketIn(CamelIn):
    issued_on: date
    destination: str = Field("", max_length=100)
    note: str = Field("", max_length=1000)


class TicketPatch(CamelIn):
    issued_on: date | None = None
    destination: str | None = Field(None, max_length=100)
    note: str | None = Field(None, max_length=1000)


class TicketOut(Camel):
    id: int
    employee_id: int
    issued_on: date
    destination: str
    note: str
    issued_by: str | None
    scheduled: date | None = None
    early: bool = False
    months_early: int = 0


class EmployeeTickets(Camel):
    info: TicketInfoOut
    history: list[TicketOut]


class TicketDueRow(Camel):
    employee_id: int
    emp_no: str
    employee_name: str
    company: CompanyRef
    department: str
    category: str
    next_due: date | None
    days_left: int | None
    overdue: bool
    last_issued: date | None


class TicketSummary(Camel):
    eligible: int
    due_soon: int
    overdue: int
    issued_this_year: int
    early_this_year: int


# ---------------------------------------------------------------- dashboard
class Slice(Camel):
    key: str
    label: str
    value: int


class DashboardOut(Camel):
    date: date
    company_id: int | None
    figures: dict[str, int]
    by_company: list[Slice]
    by_department: list[Slice]
    by_category: list[Slice]
    by_status: list[Slice]
    leave_by_type: list[Slice]
    leave_by_month: list[dict[str, Any]]
    documents_by_level: list[Slice]
    service_bands: list[Slice]
    upcoming_leaves: list[LeaveOut]


class DrillOut(Camel):
    chart: str
    key: str
    title: str
    employees: list[EmployeeOut]
    leaves: list[LeaveOut]
    documents: list[DocumentRow]


# ---------------------------------------------------------------- users, roles, permissions
class PermissionOut(Camel):
    id: int
    key: str
    label: str
    group_name: str


class RoleOut(Camel):
    id: int
    key: str
    name: str
    description: str
    is_system: bool
    permissions: list[str]
    user_count: int


class RoleIn(CamelIn):
    key: str = Field(min_length=2, max_length=50, pattern=r"^[a-z][a-z0-9_]*$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field("", max_length=1000)
    permissions: list[str] = []


class RolePatch(CamelIn):
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = Field(None, max_length=1000)
    permissions: list[str] | None = None


class UserOut(Camel):
    id: int
    username: str
    full_name: str
    email: str | None
    role: RoleRef
    is_active: bool
    last_login_at: datetime | None
    locked: bool
    companies: list[CompanyRef]
    all_companies: bool
    created_at: datetime


class UserIn(CamelIn):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9._\-]+$")
    full_name: str = Field(min_length=1, max_length=150)
    email: EmailStr | None = None
    password: str = Field(min_length=8, max_length=200)
    role_id: int
    company_ids: list[int] = []


class UserPatch(CamelIn):
    full_name: str | None = Field(None, min_length=1, max_length=150)
    email: EmailStr | None = None
    role_id: int | None = None
    is_active: bool | None = None
    unlock: bool | None = None


class ResetPasswordIn(CamelIn):
    new_password: str = Field(min_length=8, max_length=200)


class CompanyIdsIn(CamelIn):
    company_ids: list[int]


# ---------------------------------------------------------------- rules and audit
class RulesOut(Camel):
    company_id: int | None
    version: int
    inherited: bool
    config: RulesConfig
    created_at: datetime | None


class RulesIn(CamelIn):
    company_id: int | None = None
    config: RulesConfig


class AuditOut(Camel):
    id: int
    at: datetime
    user_id: int | None
    username: str | None
    action: str
    entity: str
    entity_id: int | None
    company_id: int | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    ip: str


# ---------------------------------------------------------------- reports
class LeaveHistoryRow(Camel):
    employee: EmployeeOut
    leaves: list[LeaveBrief]
    balances: list[BalanceOut]
    ticket: TicketInfoOut
    next_leave_plan: date | None


class FullReport(Camel):
    generated_at: datetime
    employees: list[EmployeeOut]
    leaves: list[LeaveOut]
    documents: list[DocumentRow]
    tickets: list[TicketOut]
    renewals: list[RenewalOut]


ReminderOut.model_rebuild()
