"""LEAVE, TICKET AND DOCUMENT RULES: the single place to change business rules.

This mirrors frontend/src/rules/leaveRules.ts. The functions are pure: they take plain values
and return plain values, so they are easy to test and never touch the database.
Values that HR can change live in RulesConfig and are saved in the rule_sets table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DocType = Literal["passport", "emirates_id", "visit_visa"]
DocLevel = Literal["expired", "urgent", "soon", "ok", "none"]
DOC_TYPES: tuple[DocType, ...] = ("passport", "emirates_id", "visit_visa")
DOC_LABEL: dict[str, str] = {"passport": "Passport", "emirates_id": "Emirates ID", "visit_visa": "Visit visa"}


class TicketCycle(BaseModel):
    A: int = Field(1, ge=1, le=5)
    B: int = Field(2, ge=1, le=5)


class TicketDefault(BaseModel):
    """Whether a new employee of each category starts as ticket-eligible (the form's default tick)."""

    A: bool = False
    B: bool = True


class RulesConfig(BaseModel):
    """Values on the Leave rules screen. Field names match the frontend (camelCase)."""

    model_config = ConfigDict(extra="forbid")

    categoryA_annualDaysPerYear: int = Field(30, ge=0, le=365)
    categoryA_prorateFirstYear: bool = True
    categoryB_daysPerCycle: int = Field(60, ge=0, le=730)
    categoryB_cycleYears: int = Field(2, ge=1, le=5)
    categoryB_grantMode: Literal["full", "accrue"] = "full"
    maxCarryForwardDays: int = Field(0, ge=0, le=365)
    emergencyDaysPerYear: int = Field(5, ge=0, le=365)
    otherDaysPerYear: int = Field(10, ge=0, le=365)
    ticketCycleYears: TicketCycle = TicketCycle()
    ticketByCategoryDefault: TicketDefault = TicketDefault()
    ticketDueWarningDays: int = Field(60, ge=0, le=365)
    passportWarnDays: int = Field(90, ge=0, le=730)
    emiratesIdWarnDays: int = Field(60, ge=0, le=730)
    visitVisaWarnDays: int = Field(30, ge=0, le=730)
    documentUrgentDays: int = Field(15, ge=0, le=365)
    countWeekends: bool = True


DEFAULT_RULES = RulesConfig()


# ---------------------------------------------------------------- plain inputs
@dataclass(frozen=True)
class EmpFacts:
    category: str  # 'A' or 'B'
    joining_date: date
    ticket_eligible: bool = False


@dataclass(frozen=True)
class LeaveFacts:
    type_code: str
    start: date
    end: date
    status: str  # pending / approved / rejected / cancelled
    id: int | None = None


@dataclass(frozen=True)
class AdjustmentFacts:
    type_code: str
    days: float
    effective_date: date


# ---------------------------------------------------------------- dates
def add_years(d: date, n: int) -> date:
    """Same day n years later; 29 Feb becomes 28 Feb in a non-leap year."""
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        return d.replace(year=d.year + n, day=28)


def leave_days(start: date, end: date, rules: RulesConfig = DEFAULT_RULES) -> int:
    """Inclusive day count: 1 Jan to 1 Jan is 1 day. The old Excel sheet left out the last day."""
    if end < start:
        return 0
    if rules.countWeekends:
        return (end - start).days + 1
    n, d = 0, start
    while d <= end:
        if d.weekday() not in (5, 6):  # UAE weekend: Saturday and Sunday
            n += 1
        d += timedelta(days=1)
    return n


def overlap_days(start: date, end: date, p_start: date, p_end: date, rules: RulesConfig) -> int:
    a, b = max(start, p_start), min(end, p_end)
    return 0 if b < a else leave_days(a, b, rules)


def months_between(a: date, b: date) -> int:
    m = (b.year - a.year) * 12 + b.month - a.month
    if b.day < a.day:
        m -= 1
    return max(0, m)


def service_text(joining: date, today: date) -> str:
    """'7 years 8 months' from the joining date."""
    m = months_between(joining, today)
    y, r = divmod(m, 12)
    parts = []
    if y:
        parts.append(f"{y} year{'s' if y > 1 else ''}")
    if r:
        parts.append(f"{r} month{'s' if r > 1 else ''}")
    return " ".join(parts) or "Less than a month"


# ---------------------------------------------------------------- entitlement periods
@dataclass(frozen=True)
class Period:
    start: date
    end: date
    label: str


def annual_period(emp: EmpFacts, today: date, rules: RulesConfig = DEFAULT_RULES) -> Period:
    if emp.category == "A":
        return Period(date(today.year, 1, 1), date(today.year, 12, 31), str(today.year))
    start = emp.joining_date
    while add_years(start, rules.categoryB_cycleYears) <= today:
        start = add_years(start, rules.categoryB_cycleYears)
    end = add_years(start, rules.categoryB_cycleYears) - timedelta(days=1)
    return Period(start, end, f"{start.isoformat()} to {end.isoformat()}")


def calendar_year(today: date) -> Period:
    return Period(date(today.year, 1, 1), date(today.year, 12, 31), str(today.year))


def annual_entitlement(emp: EmpFacts, period: Period, today: date, rules: RulesConfig = DEFAULT_RULES) -> int:
    if emp.category == "A":
        full = rules.categoryA_annualDaysPerYear
        if rules.categoryA_prorateFirstYear and emp.joining_date > period.start:
            months_left = 12 - (emp.joining_date.month - 1)  # joined in Nov -> 2 months
            return round(full * months_left / 12)
        return full
    full = rules.categoryB_daysPerCycle
    if rules.categoryB_grantMode == "full":
        return full
    total_months = rules.categoryB_cycleYears * 12
    months = min(total_months, (today.year - period.start.year) * 12 + today.month - period.start.month)
    return full * months // total_months


@dataclass(frozen=True)
class Balance:
    type_code: str
    entitled: float
    used: int
    pending: int
    adjusted: float
    remaining: float
    period: str
    period_start: date
    period_end: date


def balances(
    emp: EmpFacts,
    leaves: list[LeaveFacts],
    today: date,
    rules: RulesConfig = DEFAULT_RULES,
    adjustments: list[AdjustmentFacts] | None = None,
) -> list[Balance]:
    """Balance per leave type. remaining = entitled + adjustments - approved days (pending is shown apart)."""
    adjustments = adjustments or []
    ap = annual_period(emp, today, rules)
    yp = calendar_year(today)

    def make(code: str, entitled: float, p: Period) -> Balance:
        used = sum(
            overlap_days(lv.start, lv.end, p.start, p.end, rules)
            for lv in leaves
            if lv.type_code == code and lv.status == "approved"
        )
        pending = sum(
            overlap_days(lv.start, lv.end, p.start, p.end, rules)
            for lv in leaves
            if lv.type_code == code and lv.status == "pending"
        )
        adjusted = sum(a.days for a in adjustments if a.type_code == code and p.start <= a.effective_date <= p.end)
        return Balance(code, entitled, used, pending, adjusted, entitled + adjusted - used, p.label, p.start, p.end)

    return [
        make("annual", annual_entitlement(emp, ap, today, rules), ap),
        make("emergency", rules.emergencyDaysPerYear, yp),
        make("other", rules.otherDaysPerYear, yp),
    ]


def entitlement_for(code: str, rules: RulesConfig) -> int | None:
    """Fixed yearly allowance for non-annual types; None for types with no limit (new custom types)."""
    return {"emergency": rules.emergencyDaysPerYear, "other": rules.otherDaysPerYear}.get(code)


# ---------------------------------------------------------------- leave validation
@dataclass
class Validation:
    ok: bool
    errors: list[str]
    warnings: list[str]
    days: int


def validate_leave(
    emp: EmpFacts,
    type_code: str,
    start: date,
    end: date,
    others: list[LeaveFacts],
    rules: RulesConfig = DEFAULT_RULES,
    adjustments: list[AdjustmentFacts] | None = None,
    exclude_id: int | None = None,
) -> Validation:
    """Checks run before a leave is saved. Errors block saving; warnings are shown but allowed."""
    errors: list[str] = []
    warnings: list[str] = []
    if end < start:
        errors.append("The end date is before the start date.")
    if start < emp.joining_date:
        errors.append("The leave starts before the joining date.")
    days = leave_days(start, end, rules)
    if errors:
        return Validation(False, errors, warnings, days)
    live = [lv for lv in others if lv.id != exclude_id and lv.status in ("pending", "approved")]
    clash = next((lv for lv in live if not (end < lv.start or start > lv.end)), None)
    if clash:
        errors.append(f"Overlaps another leave ({clash.start.isoformat()} to {clash.end.isoformat()}).")
    bal = next((b for b in balances(emp, live, start, rules, adjustments) if b.type_code == type_code), None)
    if bal is not None:
        left = bal.remaining - bal.pending
        if days > left:
            warnings.append(
                f"This is {days} days but only {left:g} {type_code} days are left for {bal.period}. "
                "It will go negative."
            )
    return Validation(not errors, errors, warnings, days)


# ---------------------------------------------------------------- air tickets
@dataclass(frozen=True)
class TicketInfo:
    eligible: bool
    cycle_years: int
    next_due: date | None
    due_soon: bool
    overdue: bool
    last_issued: date | None
    issued_count: int


def ticket_info(emp: EmpFacts, issued: list[date], today: date, rules: RulesConfig = DEFAULT_RULES) -> TicketInfo:
    """Next ticket: counts from the last ticket actually issued (an early ticket moves the next one later),
    or from the joining date if none was issued yet (then: the next anniversary of joining)."""
    cycle = rules.ticketCycleYears.A if emp.category == "A" else rules.ticketCycleYears.B
    last = max(issued) if issued else None
    if not emp.ticket_eligible:
        return TicketInfo(False, cycle, None, False, False, last, len(issued))
    if last:
        due = add_years(last, cycle)
    else:
        due = add_years(emp.joining_date, cycle)
        while due < today:
            due = add_years(due, cycle)
    overdue = due < today
    due_soon = overdue or (due - today).days <= rules.ticketDueWarningDays
    return TicketInfo(True, cycle, due, due_soon, overdue, last, len(issued))


@dataclass(frozen=True)
class TicketHistoryRow:
    issued_on: date
    scheduled: date | None
    early: bool
    months_early: int


def ticket_history(emp: EmpFacts, issued: list[date], rules: RulesConfig = DEFAULT_RULES) -> list[TicketHistoryRow]:
    """Each issued ticket (newest first) with the date it was scheduled for and whether it was given early.
    For long-serving staff the first ticket on record is not their first ever, so it has no scheduled date."""
    cycle = rules.ticketCycleYears.A if emp.category == "A" else rules.ticketCycleYears.B
    out: list[TicketHistoryRow] = []
    anchor = emp.joining_date
    for i, d in enumerate(sorted(issued)):
        scheduled: date | None = add_years(anchor, cycle)
        if i == 0 and scheduled is not None and d > add_years(scheduled, 1):
            scheduled = None
        months_early = round((scheduled - d).days / 30.44) if scheduled and d < scheduled else 0
        out.append(TicketHistoryRow(d, scheduled, months_early >= 1, months_early))
        anchor = d
    return list(reversed(out))


# ---------------------------------------------------------------- documents
DOC_LEVEL_LABEL = {
    "expired": "Expired",
    "urgent": "Expires very soon",
    "soon": "Renewal due",
    "ok": "Valid",
    "none": "Not recorded",
}


def warn_days(doc_type: str, rules: RulesConfig) -> int:
    return {"passport": rules.passportWarnDays, "emirates_id": rules.emiratesIdWarnDays}.get(
        doc_type, rules.visitVisaWarnDays
    )


def document_status(
    doc_type: str, expiry: date | None, today: date, rules: RulesConfig = DEFAULT_RULES
) -> tuple[int | None, DocLevel]:
    """(days left, level). Negative days = already expired."""
    if expiry is None:
        return None, "none"
    left = (expiry - today).days
    if left < 0:
        return left, "expired"
    if left <= rules.documentUrgentDays:
        return left, "urgent"
    if left <= warn_days(doc_type, rules):
        return left, "soon"
    return left, "ok"


def next_leave_plan(category: str, end: date, rules: RulesConfig = DEFAULT_RULES) -> date:
    """End of an annual leave + the leave cycle, like the PLAN column of the HR sheet."""
    return add_years(end, 1 if category == "A" else rules.categoryB_cycleYears)
