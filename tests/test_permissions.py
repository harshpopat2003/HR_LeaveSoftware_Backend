"""Role permissions and the company filter."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Company, Employee, LeaveRequest, User, UserCompanyAccess
from app.security import create_access_token, hash_password
from tests.conftest import company_id

# (method, path, body) that each need one permission. {e} is an employee id, {l} a pending leave id.
CASES: list[tuple[str, str, str, object]] = [
    ("employees.edit", "POST", "/api/v1/employees", {}),
    ("employees.edit", "PATCH", "/api/v1/employees/{e}", {"designation": "x"}),
    ("leaves.edit", "POST", "/api/v1/leaves", {}),
    ("leaves.edit", "POST", "/api/v1/leaves/{l}/cancel", None),
    ("leaves.approve", "POST", "/api/v1/leaves/{l}/approve", None),
    ("leaves.approve", "GET", "/api/v1/approvals", None),
    (
        "documents.edit",
        "PUT",
        "/api/v1/employees/{e}/documents/passport",
        {"docNumber": "X", "expiryDate": "2030-01-01"},
    ),
    ("users.manage", "GET", "/api/v1/users", None),
    ("users.manage", "GET", "/api/v1/audit", None),
    ("roles.manage", "GET", "/api/v1/roles", None),
    ("roles.manage", "GET", "/api/v1/permissions", None),
    ("rules.edit", "PUT", "/api/v1/rules", {"config": {}}),
    ("rules.edit", "POST", "/api/v1/leave-types", {"code": "study", "name": "Study"}),
]

ROLE_PERMS = {
    "admin": {"*"},
    "manager": {"dashboard.view", "employees.view", "leaves.view", "leaves.approve", "data.export", "documents.view"},
    "hr1": {
        "dashboard.view",
        "employees.view",
        "employees.edit",
        "leaves.view",
        "leaves.edit",
        "data.export",
        "documents.view",
        "documents.edit",
    },
}


def _ids(db: Session) -> dict[str, int]:
    e = db.scalar(select(Employee.id).order_by(Employee.id).limit(1))
    lv = db.scalar(select(LeaveRequest.id).where(LeaveRequest.status == "pending").limit(1))
    assert e and lv
    return {"e": e, "l": lv}


@pytest.mark.parametrize("user", ["admin", "manager", "hr1"])
@pytest.mark.parametrize(("perm", "method", "path", "body"), CASES)
def test_permission_matrix(
    client: TestClient, db: Session, headers_for, user: str, perm: str, method: str, path: str, body: object
) -> None:
    url = path.format(**_ids(db))
    r = client.request(method, url, json=body, headers=headers_for(user))
    allowed = "*" in ROLE_PERMS[user] or perm in ROLE_PERMS[user]
    if allowed:
        assert r.status_code != 403, f"{user} should be allowed {method} {url}: {r.text}"
    else:
        assert r.status_code == 403, f"{user} should be refused {method} {url}: {r.status_code}"
        assert r.json()["error"]["code"] == "forbidden"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/employees",
        "/api/v1/dashboard",
        "/api/v1/leaves",
        "/api/v1/documents",
        "/api/v1/reports/full",
        "/api/v1/companies",
    ],
)
def test_everything_needs_sign_in(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401


def test_custom_role_without_permissions_sees_nothing(
    client: TestClient, as_admin: dict[str, str], db: Session
) -> None:
    r = client.post("/api/v1/roles", json={"key": "viewer", "name": "Viewer", "permissions": []}, headers=as_admin)
    assert r.status_code == 201
    u = User(username="nobody", full_name="No Perms", password_hash=hash_password("x" * 10), role_id=r.json()["id"])
    db.add(u)
    db.flush()
    h = {"Authorization": f"Bearer {create_access_token(u.id)}"}
    assert client.get("/api/v1/employees", headers=h).status_code == 403
    assert client.get("/api/v1/dashboard", headers=h).status_code == 403
    assert client.get("/api/v1/auth/me", headers=h).json()["permissions"] == []


@pytest.fixture
def plastic_hr(db: Session) -> dict[str, str]:
    """An HR user limited to Sabin Plastic."""
    hr = db.scalar(select(User).where(User.username == "hr2"))
    assert hr is not None
    db.add(UserCompanyAccess(user_id=hr.id, company_id=company_id(db, "SPL")))
    db.flush()
    db.expire(hr)
    return {"Authorization": f"Bearer {create_access_token(hr.id)}"}


def test_company_limited_user_only_sees_own_company(
    client: TestClient, db: Session, plastic_hr: dict[str, str]
) -> None:
    spl, ssg = company_id(db, "SPL"), company_id(db, "SSG")
    me = client.get("/api/v1/auth/me", headers=plastic_hr).json()
    assert me["allCompanies"] is False and [c["code"] for c in me["companies"]] == ["SPL"]
    assert [c["code"] for c in client.get("/api/v1/companies", headers=plastic_hr).json()] == ["SPL"]
    items = client.get("/api/v1/employees?size=500", headers=plastic_hr).json()["items"]
    assert items and {x["company"]["id"] for x in items} == {spl}
    leaves = client.get("/api/v1/leaves?size=500", headers=plastic_hr).json()["items"]
    assert leaves and {x["company"]["id"] for x in leaves} == {spl}
    assert {
        x["company"]["id"] for x in client.get("/api/v1/documents?size=500", headers=plastic_hr).json()["items"]
    } <= {spl}
    dash = client.get("/api/v1/dashboard", headers=plastic_hr).json()
    assert [s["label"] for s in dash["byCompany"]] == ["Sabin Plastic"]
    # asking for another company is refused
    assert client.get(f"/api/v1/employees?company_id={ssg}", headers=plastic_hr).status_code == 403
    assert client.get(f"/api/v1/dashboard?company_id={ssg}", headers=plastic_hr).status_code == 403


def test_company_limited_user_gets_404_for_other_companies_records(
    client: TestClient, db: Session, plastic_hr: dict[str, str]
) -> None:
    ssg = company_id(db, "SSG")
    other = db.scalar(select(Employee).where(Employee.company_id == ssg).limit(1))
    assert other is not None
    for path in (
        f"/api/v1/employees/{other.id}",
        f"/api/v1/employees/{other.id}/summary",
        f"/api/v1/employees/{other.id}/tickets",
    ):
        assert client.get(path, headers=plastic_hr).status_code == 404
    assert (
        client.patch(f"/api/v1/employees/{other.id}", json={"designation": "x"}, headers=plastic_hr).status_code == 404
    )
    lv = db.scalar(select(LeaveRequest).join(Employee).where(Employee.company_id == ssg).limit(1))
    assert lv is not None
    assert client.get(f"/api/v1/leaves/{lv.id}", headers=plastic_hr).status_code == 404
    # cannot create an employee in another company
    dept = db.scalar(select(Company).where(Company.id == ssg))
    assert dept is not None
    body = {
        "companyId": ssg,
        "departmentId": 1,
        "empNo": "ZZ1",
        "firstName": "A",
        "category": "A",
        "joiningDate": "2026-01-01",
    }
    assert client.post("/api/v1/employees", json=body, headers=plastic_hr).status_code == 404
    # HR has no rules.edit, so the group rules are refused too
    assert client.put("/api/v1/rules", json={"config": {}}, headers=plastic_hr).status_code == 403
