import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import company_id


def test_dashboard_all_companies(client: TestClient, as_manager: dict[str, str]) -> None:
    d = client.get("/api/v1/dashboard", headers=as_manager).json()
    assert d["date"] == "2026-11-27" and d["companyId"] is None
    assert d["figures"] == {
        "activeEmployees": 23,
        "onLeaveToday": 3,
        "pendingApprovals": 4,
        "documentsNeedingAction": 8,
    }
    assert {s["label"]: s["value"] for s in d["byCompany"]} == {"Sabin Plastic": 9, "Sabin Signs": 7, "Trading": 7}
    assert sum(s["value"] for s in d["byDepartment"]) == 23
    assert {s["key"]: s["value"] for s in d["byCategory"]} == {"A": 11, "B": 12}
    assert sum(s["value"] for s in d["serviceBands"]) == 23
    assert len(d["leaveByMonth"]) == 12 and d["leaveByMonth"][-1]["month"] == "2026-11"
    assert {s["key"] for s in d["documentsByLevel"]} == {"expired", "urgent", "soon"}
    assert sum(s["value"] for s in d["documentsByLevel"]) == 8
    assert all("2026-11-27" < x["startDate"] <= "2026-12-27" for x in d["upcomingLeaves"])


def test_dashboard_one_company(client: TestClient, as_manager: dict[str, str], db: Session) -> None:
    trd = company_id(db, "TRD")
    d = client.get(f"/api/v1/dashboard?company_id={trd}", headers=as_manager).json()
    assert d["companyId"] == trd and d["figures"]["activeEmployees"] == 7
    assert [s["label"] for s in d["byCompany"]] == ["Trading"]


@pytest.mark.parametrize(
    ("chart", "key", "what", "count"),
    [
        ("department", "Production", "employees", 6),
        ("category", "B", "employees", 12),
        ("service", "10plus", "employees", None),
        ("documents", "expired", "documents", 2),
        ("documents", "", "documents", 8),
        ("on_leave", "", "leaves", 3),
        ("pending", "", "leaves", 4),
        ("leave_month", "2026-11", "leaves", None),
        ("leave_type", "annual", "leaves", None),
    ],
)
def test_drill_down(
    client: TestClient, as_manager: dict[str, str], chart: str, key: str, what: str, count: int | None
) -> None:
    r = client.get(f"/api/v1/dashboard/drill?chart={chart}&key={key}", headers=as_manager)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["title"]
    rows = d[what]
    assert len(rows) == count if count is not None else len(rows) > 0


def test_drill_company_and_bad_input(client: TestClient, as_manager: dict[str, str], db: Session) -> None:
    spl = company_id(db, "SPL")
    d = client.get(f"/api/v1/dashboard/drill?chart=company&key={spl}", headers=as_manager).json()
    assert len(d["employees"]) == 9 and d["title"] == "Employees of Sabin Plastic"
    assert client.get("/api/v1/dashboard/drill?chart=nope", headers=as_manager).status_code == 422
    assert client.get("/api/v1/dashboard/drill?chart=leave_month&key=bad", headers=as_manager).status_code == 422


def test_on_leave(client: TestClient, as_manager: dict[str, str]) -> None:
    today = client.get("/api/v1/dashboard/on-leave", headers=as_manager).json()
    assert len(today) == 3 and all(x["startDate"] <= "2026-11-27" <= x["endDate"] for x in today)
    jan = client.get("/api/v1/dashboard/on-leave?date=2015-03-01", headers=as_manager).json()
    assert all(x["startDate"] <= "2015-03-01" <= x["endDate"] for x in jan)


def test_reports(client: TestClient, as_manager: dict[str, str], db: Session) -> None:
    emps = client.get("/api/v1/reports/employees", headers=as_manager).json()
    assert len(emps) == 23
    lv = client.get("/api/v1/reports/leaves?status=approved", headers=as_manager).json()
    assert len(lv) == 64 and all(x["status"] == "approved" for x in lv)
    ranged = client.get("/api/v1/reports/leaves?from=2026-01-01&to=2026-12-31", headers=as_manager).json()
    assert all(x["endDate"] >= "2026-01-01" for x in ranged)
    docs = client.get("/api/v1/reports/documents", headers=as_manager).json()
    assert len(docs) == 46
    hist = client.get("/api/v1/reports/leave-history", headers=as_manager).json()
    row = next(r for r in hist if r["employee"]["empNo"] == "SPW013")
    starts = [x["startDate"] for x in row["leaves"]]
    assert starts == sorted(starts) and all(x["status"] == "approved" for x in row["leaves"])
    assert row["balances"][0]["remaining"] == 14.0 and row["nextLeavePlan"] == "2027-10-20"
    full = client.get("/api/v1/reports/full", headers=as_manager).json()
    assert (
        len(full["employees"]) == 23
        and len(full["leaves"]) == 69
        and len(full["tickets"]) == 13
        and len(full["renewals"]) == 3
    )
    one = client.get(f"/api/v1/reports/full?company_id={company_id(db, 'SSG')}", headers=as_manager).json()
    assert {e["company"]["code"] for e in one["employees"]} == {"SSG"}


def test_reports_need_export_permission(client: TestClient, db: Session, as_admin: dict[str, str]) -> None:
    from sqlalchemy import select

    from app.models import User
    from app.security import create_access_token, hash_password

    role = client.post(
        "/api/v1/roles", json={"key": "reader", "name": "Reader", "permissions": ["employees.view"]}, headers=as_admin
    ).json()
    u = User(username="reader", full_name="Reader", password_hash=hash_password("readerpass"), role_id=role["id"])
    db.add(u)
    db.flush()
    h = {"Authorization": f"Bearer {create_access_token(u.id)}"}
    assert client.get("/api/v1/employees", headers=h).status_code == 200
    for path in ("full", "leave-history", "employees", "leaves", "documents"):
        assert client.get(f"/api/v1/reports/{path}", headers=h).status_code == 403
    assert db.scalar(select(User.id).where(User.username == "reader"))
