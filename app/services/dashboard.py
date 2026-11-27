"""Dashboard figures and charts, and the drill-down behind each chart."""

from collections import Counter
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import schemas as S
from app.clock import today
from app.errors import invalid
from app.models import Company, Employee, LeaveRequest, LeaveType
from app.rules import leave_rules as R
from app.services import documents, leaves, present
from app.services.employees import filtered
from app.services.rules import RulesCache

SERVICE_BANDS = (
    ("lt1", "Under 1 year", 0, 12),
    ("1to3", "1 to 3 years", 12, 36),
    ("3to5", "3 to 5 years", 36, 60),
    ("5to10", "5 to 10 years", 60, 120),
    ("10plus", "10 years or more", 120, 10**6),
)
LEVEL_LABEL = {"expired": "Expired", "urgent": "Expires within 15 days", "soon": "Renewal due"}


def _active(db: Session, companies: list[int] | None) -> list[Employee]:
    return list(db.scalars(filtered(companies, status="active").order_by(Employee.emp_no)).unique())


def _month_start(d: date, back: int) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def _month_end(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1) - timedelta(days=1)


def _band(e: Employee, t: date) -> str:
    m = R.months_between(e.joining_date, t)
    return next(k for k, _, lo, hi in SERVICE_BANDS if lo <= m < hi)


def _on_leave(db: Session, companies: list[int] | None, on: date) -> list[LeaveRequest]:
    stmt = leaves.list_stmt(companies, status="approved").where(
        LeaveRequest.start_date <= on, LeaveRequest.end_date >= on
    )
    return list(db.scalars(stmt.order_by(LeaveRequest.start_date)).unique())


def build(db: Session, companies: list[int] | None, company_id: int | None) -> S.DashboardOut:
    t = today()
    rules = RulesCache(db)
    emps = _active(db, companies)
    pending = db.scalar(select(func.count()).select_from(leaves.list_stmt(companies, status="pending").subquery())) or 0
    alerts = documents.rows(db, companies, levels=documents.ALERT_LEVELS)
    on_leave = _on_leave(db, companies, t)

    start = _month_start(t, 11)
    yr_start, yr_end = date(t.year, 1, 1), date(t.year, 12, 31)
    rows = db.execute(
        leaves.list_stmt(companies, status="approved", date_from=start, date_to=_month_end(t))
        .with_only_columns(LeaveRequest.start_date, LeaveRequest.end_date, LeaveType.code, Employee.company_id)
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
    ).all()
    months = []
    for i in range(11, -1, -1):
        ms = _month_start(t, i)
        me = _month_end(ms)
        c: Counter[str] = Counter()
        for s, e, code, cid in rows:
            c[code] += R.overlap_days(s, e, ms, me, rules.get(cid))
        months.append(
            {
                "month": ms.strftime("%Y-%m"),
                "label": ms.strftime("%b %Y"),
                **{k: c[k] for k in ("annual", "emergency", "other")},
            }
        )
    yr_rows = db.execute(
        leaves.list_stmt(companies, status="approved", date_from=yr_start, date_to=yr_end)
        .with_only_columns(
            LeaveRequest.start_date, LeaveRequest.end_date, LeaveType.code, LeaveType.name, Employee.company_id
        )
        .join(LeaveType, LeaveType.id == LeaveRequest.leave_type_id)
    ).all()
    by_type: Counter[str] = Counter()
    names: dict[str, str] = {}
    for s, e, code, name, cid in yr_rows:
        by_type[code] += R.overlap_days(s, e, yr_start, yr_end, rules.get(cid))
        names[code] = name

    upcoming = list(
        db.scalars(
            leaves.list_stmt(companies, date_from=t)
            .where(
                LeaveRequest.status.in_(("approved", "pending")),
                LeaveRequest.start_date > t,
                LeaveRequest.start_date <= t + timedelta(days=30),
            )
            .order_by(LeaveRequest.start_date)
            .limit(10)
        ).unique()
    )
    comp_q = select(Company).order_by(Company.id)
    if companies is not None:
        comp_q = comp_q.where(Company.id.in_(companies))
    comp_count = Counter(e.company_id for e in emps)
    dept = Counter(e.department.name for e in emps)
    bands = Counter(_band(e, t) for e in emps)
    levels = Counter(a.level for a in alerts)
    return S.DashboardOut(
        date=t,
        company_id=company_id,
        figures={
            "activeEmployees": len(emps),
            "onLeaveToday": len({lv.employee_id for lv in on_leave}),
            "pendingApprovals": pending,
            "documentsNeedingAction": len(alerts),
        },
        by_company=[S.Slice(key=str(c.id), label=c.name, value=comp_count[c.id]) for c in db.scalars(comp_q)],
        by_department=[S.Slice(key=k, label=k, value=v) for k, v in sorted(dept.items(), key=lambda x: (-x[1], x[0]))],
        by_category=[
            S.Slice(key=k, label=f"Category {k}", value=sum(1 for e in emps if e.category == k)) for k in ("A", "B")
        ],
        by_status=[
            S.Slice(key="on_leave", label="On leave today", value=len({lv.employee_id for lv in on_leave})),
            S.Slice(key="working", label="Working", value=len(emps) - len({lv.employee_id for lv in on_leave})),
        ],
        leave_by_type=[S.Slice(key=k, label=names.get(k, k), value=v) for k, v in sorted(by_type.items())],
        leave_by_month=months,
        documents_by_level=[S.Slice(key=k, label=v, value=levels[k]) for k, v in LEVEL_LABEL.items()],
        service_bands=[S.Slice(key=k, label=label, value=bands[k]) for k, label, _, _ in SERVICE_BANDS],
        upcoming_leaves=[present.leave_out(lv) for lv in upcoming],
    )


CHARTS = (
    "company",
    "department",
    "category",
    "service",
    "documents",
    "leave_type",
    "leave_month",
    "on_leave",
    "pending",
)


def drill(db: Session, companies: list[int] | None, chart: str, key: str) -> S.DrillOut:
    """Rows behind one bar or slice of a dashboard chart."""
    t = today()
    rules = RulesCache(db)
    if chart not in CHARTS:
        raise invalid(f"Unknown chart '{chart}'.", {"field": "chart", "allowed": list(CHARTS)})
    emps: list[Employee] = []
    lvs: list[LeaveRequest] = []
    docs: list[S.DocumentRow] = []
    title = ""
    if chart in ("company", "department", "category", "service"):
        pool = _active(db, companies)
        if chart == "company":
            emps = [e for e in pool if str(e.company_id) == key]
            title = f"Employees of {emps[0].company.name}" if emps else "Employees"
        elif chart == "department":
            emps = [e for e in pool if e.department.name == key]
            title = f"Employees in {key}"
        elif chart == "category":
            emps = [e for e in pool if e.category == key]
            title = f"Category {key} employees"
        else:
            emps = [e for e in pool if _band(e, t) == key]
            title = next((label for k, label, _, _ in SERVICE_BANDS if k == key), key)
    elif chart == "documents":
        docs = documents.rows(db, companies, levels=(key,) if key in documents.ALERT_LEVELS else documents.ALERT_LEVELS)
        title = LEVEL_LABEL.get(key, "Documents needing action")
    elif chart == "leave_type":
        lvs = list(
            db.scalars(
                leaves.list_stmt(
                    companies,
                    status="approved",
                    type_code=key,
                    date_from=date(t.year, 1, 1),
                    date_to=date(t.year, 12, 31),
                ).order_by(LeaveRequest.start_date.desc())
            ).unique()
        )
        title = f"Approved {key} leave in {t.year}"
    elif chart == "leave_month":
        try:
            y, m = (int(x) for x in key.split("-"))
            ms = date(y, m, 1)
        except ValueError as exc:
            raise invalid("Month must look like 2026-03.", {"field": "key"}) from exc
        lvs = list(
            db.scalars(
                leaves.list_stmt(companies, status="approved", date_from=ms, date_to=_month_end(ms)).order_by(
                    LeaveRequest.start_date
                )
            ).unique()
        )
        title = f"Leave in {ms.strftime('%B %Y')}"
    elif chart == "on_leave":
        lvs = _on_leave(db, companies, t)
        title = "On leave today"
    else:
        lvs = list(db.scalars(leaves.list_stmt(companies, status="pending").order_by(LeaveRequest.start_date)).unique())
        title = "Waiting for approval"
    return S.DrillOut(
        chart=chart,
        key=key,
        title=title,
        employees=[present.employee_out(e, t, rules) for e in emps],
        leaves=[present.leave_out(lv) for lv in lvs],
        documents=docs,
    )


def on_leave(db: Session, companies: list[int] | None, on: date) -> list[S.LeaveOut]:
    return [present.leave_out(lv) for lv in _on_leave(db, companies, on)]
