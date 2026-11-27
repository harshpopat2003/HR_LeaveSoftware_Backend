"""Pure rule functions: no database."""

from datetime import date

import pytest
from pydantic import ValidationError

from app.rules import leave_rules as R

A = R.EmpFacts("A", date(2020, 3, 15), True)
B = R.EmpFacts("B", date(2019, 9, 5), True)


def lv(code: str, s: str, e: str, status: str = "approved", id: int | None = None) -> R.LeaveFacts:
    return R.LeaveFacts(code, date.fromisoformat(s), date.fromisoformat(e), status, id)


@pytest.mark.parametrize(
    ("d", "n", "expected"),
    [
        (date(2024, 2, 29), 1, date(2025, 2, 28)),
        (date(2024, 2, 29), 4, date(2028, 2, 29)),
        (date(2023, 6, 1), 2, date(2025, 6, 1)),
    ],
)
def test_add_years(d: date, n: int, expected: date) -> None:
    assert R.add_years(d, n) == expected


def test_leave_days_inclusive_and_weekends() -> None:
    assert R.leave_days(date(2026, 1, 1), date(2026, 1, 1)) == 1
    assert R.leave_days(date(2026, 1, 1), date(2026, 1, 31)) == 31
    assert R.leave_days(date(2026, 1, 5), date(2026, 1, 1)) == 0
    no_weekends = R.RulesConfig(countWeekends=False)
    # Mon 5 Jan 2026 to Sun 11 Jan 2026: 5 working days
    assert R.leave_days(date(2026, 1, 5), date(2026, 1, 11), no_weekends) == 5


def test_overlap_and_months() -> None:
    assert (
        R.overlap_days(date(2025, 12, 25), date(2026, 1, 5), date(2026, 1, 1), date(2026, 12, 31), R.DEFAULT_RULES) == 5
    )
    assert (
        R.overlap_days(date(2025, 1, 1), date(2025, 1, 5), date(2026, 1, 1), date(2026, 12, 31), R.DEFAULT_RULES) == 0
    )
    assert R.months_between(date(2020, 3, 15), date(2020, 4, 14)) == 0
    assert R.months_between(date(2020, 3, 15), date(2020, 4, 15)) == 1
    assert R.months_between(date(2021, 1, 1), date(2020, 1, 1)) == 0


@pytest.mark.parametrize(
    ("joined", "today", "text"),
    [
        (date(2026, 11, 1), date(2026, 11, 27), "Less than a month"),
        (date(2025, 11, 27), date(2026, 11, 27), "1 year"),
        (date(2019, 3, 1), date(2026, 11, 27), "7 years 8 months"),
        (date(2026, 9, 27), date(2026, 11, 27), "2 months"),
    ],
)
def test_service_text(joined: date, today: date, text: str) -> None:
    assert R.service_text(joined, today) == text


def test_category_a_calendar_year_and_prorate() -> None:
    p = R.annual_period(A, date(2026, 6, 1))
    assert (p.start, p.end, p.label) == (date(2026, 1, 1), date(2026, 12, 31), "2026")
    assert R.annual_entitlement(A, p, date(2026, 6, 1)) == 30
    new = R.EmpFacts("A", date(2026, 11, 2))
    assert (
        R.annual_entitlement(new, R.annual_period(new, date(2026, 11, 27)), date(2026, 11, 27)) == 5
    )  # 2 of 12 months
    no_pro = R.RulesConfig(categoryA_prorateFirstYear=False)
    assert R.annual_entitlement(new, R.annual_period(new, date(2026, 11, 27), no_pro), date(2026, 11, 27), no_pro) == 30


def test_category_b_cycles_from_joining() -> None:
    p = R.annual_period(B, date(2026, 11, 27))
    assert (p.start, p.end) == (date(2025, 9, 5), date(2027, 9, 4))
    assert R.annual_entitlement(B, p, date(2026, 11, 27)) == 60
    accrue = R.RulesConfig(categoryB_grantMode="accrue")
    # 14 of 24 months into the cycle
    assert R.annual_entitlement(B, p, date(2026, 11, 27), accrue) == 35
    # first day of a new cycle
    assert R.annual_period(B, date(2027, 9, 5)).start == date(2027, 9, 5)


def test_balances_used_pending_adjusted() -> None:
    leaves = [
        lv("annual", "2026-02-01", "2026-02-10"),  # 10 approved
        lv("annual", "2026-12-20", "2026-12-24", "pending"),  # 5 pending
        lv("annual", "2025-12-30", "2026-01-02"),  # 2 of 4 days fall in 2026
        lv("annual", "2026-03-01", "2026-03-05", "rejected"),
        lv("emergency", "2026-05-01", "2026-05-02"),
    ]
    adj = [R.AdjustmentFacts("annual", 2.5, date(2026, 1, 1)), R.AdjustmentFacts("annual", 9, date(2025, 1, 1))]
    bal = {b.type_code: b for b in R.balances(A, leaves, date(2026, 11, 27), adjustments=adj)}
    assert bal["annual"].used == 12
    assert bal["annual"].pending == 5
    assert bal["annual"].adjusted == 2.5
    assert bal["annual"].remaining == 30 + 2.5 - 12
    assert bal["emergency"].used == 2 and bal["emergency"].remaining == 3
    assert bal["other"].remaining == 10


def test_validate_leave() -> None:
    others = [
        lv("annual", "2026-03-01", "2026-03-10", id=1),
        lv("annual", "2026-04-01", "2026-04-03", "rejected", id=2),
    ]
    ok = R.validate_leave(A, "annual", date(2026, 4, 1), date(2026, 4, 5), others)
    assert ok.ok and ok.days == 5 and not ok.warnings
    clash = R.validate_leave(A, "annual", date(2026, 3, 10), date(2026, 3, 12), others)
    assert not clash.ok and "Overlaps" in clash.errors[0]
    # editing leave 1 itself is not a clash
    assert R.validate_leave(A, "annual", date(2026, 3, 2), date(2026, 3, 9), others, exclude_id=1).ok
    back = R.validate_leave(A, "annual", date(2026, 3, 9), date(2026, 3, 1), others)
    assert not back.ok and "before the start" in back.errors[0]
    early = R.validate_leave(A, "annual", date(2020, 1, 1), date(2020, 1, 2), [])
    assert not early.ok and "joining date" in early.errors[0]
    long = R.validate_leave(A, "annual", date(2026, 6, 1), date(2026, 7, 31), others)
    assert long.ok and "go negative" in long.warnings[0]
    custom = R.validate_leave(A, "study", date(2026, 6, 1), date(2026, 7, 31), others)
    assert custom.ok and not custom.warnings


def test_ticket_info_from_last_ticket_or_joining() -> None:
    today = date(2026, 11, 27)
    info = R.ticket_info(B, [date(2024, 9, 10), date(2025, 11, 3)], today)
    assert info.next_due == date(2027, 11, 3) and not info.due_soon and info.issued_count == 2
    none_yet = R.ticket_info(R.EmpFacts("A", date(2020, 12, 20), True), [], today)
    assert none_yet.next_due == date(2026, 12, 20) and none_yet.due_soon and not none_yet.overdue
    late = R.ticket_info(R.EmpFacts("A", date(2020, 1, 1), True), [date(2025, 10, 1)], today)
    assert late.overdue and late.due_soon
    not_eligible = R.ticket_info(R.EmpFacts("A", date(2020, 1, 1), False), [], today)
    assert not not_eligible.eligible and not_eligible.next_due is None


def test_ticket_history_marks_early_tickets() -> None:
    rows = R.ticket_history(B, [date(2024, 9, 10), date(2025, 11, 3)])
    assert rows[0].issued_on == date(2025, 11, 3)
    assert rows[0].scheduled == date(2026, 9, 10) and rows[0].early and rows[0].months_early == 10
    assert rows[1].scheduled is None  # first ticket on record for long-serving staff
    on_time = R.ticket_history(R.EmpFacts("A", date(2024, 1, 10), True), [date(2025, 1, 12)])
    assert on_time[0].scheduled == date(2025, 1, 10) and not on_time[0].early


@pytest.mark.parametrize(
    ("doc", "days", "level"),
    [
        ("passport", -1, "expired"),
        ("passport", 0, "urgent"),
        ("passport", 15, "urgent"),
        ("passport", 90, "soon"),
        ("passport", 91, "ok"),
        ("emirates_id", 60, "soon"),
        ("emirates_id", 61, "ok"),
        ("visit_visa", 30, "soon"),
        ("visit_visa", 31, "ok"),
    ],
)
def test_document_status(doc: str, days: int, level: str) -> None:
    from datetime import timedelta

    today = date(2026, 11, 27)
    assert R.document_status(doc, today + timedelta(days=days), today) == (days, level)


def test_document_status_none_and_plan() -> None:
    assert R.document_status("passport", None, date(2026, 1, 1)) == (None, "none")
    assert R.next_leave_plan("A", date(2026, 3, 1)) == date(2027, 3, 1)
    assert R.next_leave_plan("B", date(2026, 3, 1)) == date(2028, 3, 1)


def test_rules_config_rejects_bad_values() -> None:
    with pytest.raises(ValidationError):
        R.RulesConfig(categoryA_annualDaysPerYear=-1)
    with pytest.raises(ValidationError):
        R.RulesConfig.model_validate({"unknownField": 1})
    assert R.RulesConfig.model_validate(R.DEFAULT_RULES.model_dump()) == R.DEFAULT_RULES
