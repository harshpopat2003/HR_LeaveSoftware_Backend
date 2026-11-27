"""Users, roles, rules and the audit trail."""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Role, User
from tests.conftest import company_id


def _role_id(db: Session, key: str) -> int:
    rid = db.scalar(select(Role.id).where(Role.key == key))
    assert rid is not None
    return rid


def test_users_crud(client: TestClient, as_admin: dict[str, str], db: Session) -> None:
    users = client.get("/api/v1/users", headers=as_admin).json()
    assert [u["username"] for u in users] == ["admin", "manager", "hr1", "hr2"]
    assert all("password" not in str(u).lower() for u in users)
    body = {
        "username": "hr3",
        "fullName": "HR Officer 3",
        "email": "hr3@example.com",
        "password": "longenough1",
        "roleId": _role_id(db, "hr"),
        "companyIds": [company_id(db, "TRD")],
    }
    r = client.post("/api/v1/users", json=body, headers=as_admin)
    assert r.status_code == 201, r.text
    u = r.json()
    assert u["allCompanies"] is False and [c["code"] for c in u["companies"]] == ["TRD"]
    assert client.post("/api/v1/users", json=body | {"email": "other@example.com"}, headers=as_admin).status_code == 409
    assert (
        client.post(
            "/api/v1/users", json=body | {"username": "HR3", "email": "x@example.com"}, headers=as_admin
        ).status_code
        == 409
    )
    assert (
        client.post("/api/v1/users", json=body | {"username": "hr4", "password": "short"}, headers=as_admin).status_code
        == 422
    )
    assert (
        client.post("/api/v1/users", json=body | {"username": "hr4", "email": "bad"}, headers=as_admin).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/users", json=body | {"username": "hr4", "email": None, "roleId": 9999}, headers=as_admin
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/users", json=body | {"username": "hr4", "email": None, "companyIds": [9999]}, headers=as_admin
        ).status_code
        == 422
    )
    url = f"/api/v1/users/{u['id']}"
    assert client.get(url, headers=as_admin).json()["username"] == "hr3"
    patched = client.patch(
        url, json={"fullName": "HR Three", "roleId": _role_id(db, "manager")}, headers=as_admin
    ).json()
    assert patched["fullName"] == "HR Three" and patched["role"]["key"] == "manager"
    all_c = client.put(f"{url}/companies", json={"companyIds": []}, headers=as_admin).json()
    assert all_c["allCompanies"] is True
    two = client.put(
        f"{url}/companies", json={"companyIds": [company_id(db, "SPL"), company_id(db, "SSG")]}, headers=as_admin
    ).json()
    assert [c["code"] for c in two["companies"]] == ["SPL", "SSG"]
    assert client.get("/api/v1/users/99999", headers=as_admin).status_code == 404


def test_password_reset_and_unlock(client: TestClient, as_admin: dict[str, str], db: Session) -> None:
    hr2 = db.scalar(select(User).where(User.username == "hr2"))
    assert hr2 is not None
    for _ in range(5):
        client.post("/api/v1/auth/login", json={"username": "hr2", "password": "bad"})
    assert client.get(f"/api/v1/users/{hr2.id}", headers=as_admin).json()["locked"] is True
    assert client.patch(f"/api/v1/users/{hr2.id}", json={"unlock": True}, headers=as_admin).json()["locked"] is False
    assert (
        client.post(f"/api/v1/users/{hr2.id}/reset-password", json={"newPassword": "x"}, headers=as_admin).status_code
        == 422
    )
    assert (
        client.post(
            f"/api/v1/users/{hr2.id}/reset-password", json={"newPassword": "reset-pass-1"}, headers=as_admin
        ).status_code
        == 204
    )
    assert client.post("/api/v1/auth/login", json={"username": "hr2", "password": "reset-pass-1"}).status_code == 200


def test_last_admin_is_protected(client: TestClient, as_admin: dict[str, str], db: Session) -> None:
    admin = db.scalar(select(User).where(User.username == "admin"))
    assert admin is not None
    r = client.patch(f"/api/v1/users/{admin.id}", json={"isActive": False}, headers=as_admin)
    assert r.status_code == 409
    r = client.patch(f"/api/v1/users/{admin.id}", json={"roleId": _role_id(db, "hr")}, headers=as_admin)
    assert r.status_code == 409 and "Administrator" in r.json()["error"]["message"]
    # with a second admin, the first can be moved, but nobody can switch themselves off
    mgr = db.scalar(select(User).where(User.username == "manager"))
    assert mgr is not None
    client.patch(f"/api/v1/users/{mgr.id}", json={"roleId": _role_id(db, "administrator")}, headers=as_admin)
    assert client.patch(f"/api/v1/users/{admin.id}", json={"isActive": False}, headers=as_admin).status_code == 409
    assert (
        client.patch(f"/api/v1/users/{admin.id}", json={"roleId": _role_id(db, "hr")}, headers=as_admin).status_code
        == 200
    )


def test_deactivating_a_user_ends_their_sessions(client: TestClient, as_admin: dict[str, str], db: Session) -> None:
    assert client.post("/api/v1/auth/login", json={"username": "hr2", "password": "demo123"}).status_code == 200
    hr2 = db.scalar(select(User.id).where(User.username == "hr2"))
    assert client.patch(f"/api/v1/users/{hr2}", json={"isActive": False}, headers=as_admin).status_code == 200
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_roles(client: TestClient, as_admin: dict[str, str], db: Session) -> None:
    roles = {r["key"]: r for r in client.get("/api/v1/roles", headers=as_admin).json()}
    assert set(roles) == {"administrator", "manager", "hr"}
    assert len(roles["administrator"]["permissions"]) == 12 and roles["hr"]["userCount"] == 2
    assert (
        "leaves.approve" in roles["manager"]["permissions"] and "employees.edit" not in roles["manager"]["permissions"]
    )
    perms = client.get("/api/v1/permissions", headers=as_admin).json()
    assert len(perms) == 12
    r = client.post(
        "/api/v1/roles",
        json={"key": "auditor", "name": "Auditor", "permissions": ["dashboard.view", "data.export"]},
        headers=as_admin,
    )
    assert r.status_code == 201 and r.json()["isSystem"] is False
    assert (
        client.post("/api/v1/roles", json={"key": "auditor", "name": "Auditor 2"}, headers=as_admin).status_code == 409
    )
    assert (
        client.post(
            "/api/v1/roles", json={"key": "x2", "name": "X", "permissions": ["fly"]}, headers=as_admin
        ).status_code
        == 422
    )
    rid = r.json()["id"]
    p = client.patch(
        f"/api/v1/roles/{rid}", json={"permissions": ["dashboard.view"], "description": "Reads"}, headers=as_admin
    ).json()
    assert p["permissions"] == ["dashboard.view"] and p["description"] == "Reads"
    assert client.patch(f"/api/v1/roles/{rid}", json={"name": "Manager"}, headers=as_admin).status_code == 409
    # manager and HR permissions can be changed, the administrator's cannot
    mgr = roles["manager"]["id"]
    assert (
        client.patch(
            f"/api/v1/roles/{mgr}",
            json={"permissions": roles["manager"]["permissions"] + ["leaves.edit"]},
            headers=as_admin,
        ).status_code
        == 200
    )
    assert (
        client.patch(
            f"/api/v1/roles/{roles['administrator']['id']}", json={"permissions": []}, headers=as_admin
        ).status_code
        == 409
    )
    assert client.delete(f"/api/v1/roles/{mgr}", headers=as_admin).status_code == 409
    u = client.post(
        "/api/v1/users",
        json={"username": "aud1", "fullName": "A", "password": "longenough1", "roleId": rid},
        headers=as_admin,
    ).json()
    assert client.delete(f"/api/v1/roles/{rid}", headers=as_admin).status_code == 409
    client.patch(f"/api/v1/users/{u['id']}", json={"roleId": mgr}, headers=as_admin)
    assert client.delete(f"/api/v1/roles/{rid}", headers=as_admin).status_code == 204
    assert client.delete(f"/api/v1/roles/{rid}", headers=as_admin).status_code == 404
    assert client.patch("/api/v1/roles/99999", json={"name": "x"}, headers=as_admin).status_code == 404


def test_permission_change_applies_at_once(
    client: TestClient, as_admin: dict[str, str], as_manager: dict[str, str], db: Session
) -> None:
    assert client.get("/api/v1/users", headers=as_manager).status_code == 403
    mgr = _role_id(db, "manager")
    current = next(r for r in client.get("/api/v1/roles", headers=as_admin).json() if r["key"] == "manager")[
        "permissions"
    ]
    client.patch(f"/api/v1/roles/{mgr}", json={"permissions": current + ["users.manage"]}, headers=as_admin)
    db.expire_all()
    assert client.get("/api/v1/users", headers=as_manager).status_code == 200


def test_rules_versions_and_company_override(
    client: TestClient, as_admin: dict[str, str], as_hr: dict[str, str], emp, db: Session
) -> None:
    g = client.get("/api/v1/rules", headers=as_hr).json()
    assert g["version"] == 1 and g["companyId"] is None and g["config"]["categoryA_annualDaysPerYear"] == 30
    cfg = g["config"] | {"categoryB_daysPerCycle": 70}
    r = client.put("/api/v1/rules", json={"config": cfg}, headers=as_admin)
    assert r.status_code == 200 and r.json()["version"] == 2
    bad = client.put("/api/v1/rules", json={"config": cfg | {"categoryB_cycleYears": 0}}, headers=as_admin)
    assert bad.status_code == 422
    assert client.put("/api/v1/rules", json={"config": cfg | {"typo": 1}}, headers=as_admin).status_code == 422
    e = emp("SPW013")
    assert client.get(f"/api/v1/employees/{e.id}/balances", headers=as_hr).json()[0]["entitled"] == 70
    # Sabin Plastic gets its own rule: 80 days, the other companies keep the group rule
    spl = company_id(db, "SPL")
    own = client.put(
        "/api/v1/rules", json={"companyId": spl, "config": cfg | {"categoryB_daysPerCycle": 80}}, headers=as_admin
    ).json()
    assert own["version"] == 1 and own["inherited"] is False
    assert client.get(f"/api/v1/rules?company_id={spl}", headers=as_hr).json()["config"]["categoryB_daysPerCycle"] == 80
    ssg = client.get(f"/api/v1/rules?company_id={company_id(db, 'SSG')}", headers=as_hr).json()
    assert ssg["inherited"] is True and ssg["config"]["categoryB_daysPerCycle"] == 70
    assert client.get(f"/api/v1/employees/{e.id}/balances", headers=as_hr).json()[0]["entitled"] == 80
    hist = client.get("/api/v1/rules/history", headers=as_admin).json()
    assert [h["version"] for h in hist] == [2, 1]
    assert len(client.get(f"/api/v1/rules/history?company_id={spl}", headers=as_admin).json()) == 1
    assert client.put("/api/v1/rules", json={"companyId": 9999, "config": cfg}, headers=as_admin).status_code == 404


def test_audit_trail(client: TestClient, as_admin: dict[str, str], as_hr: dict[str, str], emp) -> None:
    e = emp("SPE156")
    client.patch(f"/api/v1/employees/{e.id}", json={"designation": "Chief Accountant"}, headers=as_hr)
    page = client.get(f"/api/v1/audit?entity=employee&entity_id={e.id}", headers=as_admin).json()
    assert page["total"] == 1
    row = page["items"][0]
    assert (
        row["action"] == "update" and row["username"] == "hr1" and row["after"] == {"designation": "Chief Accountant"}
    )
    assert row["before"] == {"designation": "Accountant"} and row["ip"] == "testclient"
    assert client.get("/api/v1/audit?since=2000-01-01T00:00:00Z&size=1", headers=as_admin).json()["total"] >= 1
