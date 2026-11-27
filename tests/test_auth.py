from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.clock import now_utc
from app.models import AuditLog, RefreshToken, User
from app.security import create_access_token, decode_access_token

LOGIN = "/api/v1/auth/login"


def login(client: TestClient, username: str = "hr1", password: str = "demo123"):
    return client.post(LOGIN, json={"username": username, "password": password})


def test_login_returns_token_user_and_cookie(client: TestClient, db: Session) -> None:
    r = login(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tokenType"] == "bearer" and body["expiresIn"] == 900
    assert decode_access_token(body["accessToken"]) == body["user"]["id"]
    assert body["user"]["role"]["key"] == "hr"
    assert "employees.edit" in body["user"]["permissions"] and "roles.manage" not in body["user"]["permissions"]
    assert body["user"]["allCompanies"] is True and len(body["user"]["companies"]) == 3
    cookie = r.headers["set-cookie"]
    assert (
        "hr_refresh=" in cookie
        and "HttpOnly" in cookie
        and "Path=/api/v1/auth" in cookie
        and "SameSite=strict" in cookie
    )
    assert "password" not in r.text.lower().replace("change-password", "")
    u = db.scalar(select(User).where(User.username == "hr1"))
    assert u is not None and u.last_login_at is not None
    assert db.scalar(select(AuditLog).where(AuditLog.action == "login", AuditLog.user_id == u.id)) is not None


def test_username_is_case_insensitive(client: TestClient) -> None:
    assert login(client, "HR1").status_code == 200


def test_wrong_password_and_unknown_user_look_the_same(client: TestClient) -> None:
    a = login(client, "hr1", "nope")
    b = login(client, "nobody", "nope")
    assert a.status_code == b.status_code == 401
    assert a.json() == b.json()
    assert a.json()["error"]["code"] == "invalid_login"


def test_lockout_after_five_failures(client: TestClient, db: Session) -> None:
    for _ in range(5):
        assert login(client, "hr2", "wrong").status_code == 401
    r = login(client, "hr2", "demo123")
    assert r.status_code == 423 and r.json()["error"]["code"] == "locked"
    db.execute(update(User).where(User.username == "hr2").values(locked_until=now_utc() - timedelta(seconds=1)))
    assert login(client, "hr2", "demo123").status_code == 200


def test_inactive_user_cannot_sign_in_or_use_token(client: TestClient, db: Session) -> None:
    u = db.scalar(select(User).where(User.username == "hr2"))
    assert u is not None
    token = create_access_token(u.id)
    u.is_active = False
    db.flush()
    assert login(client, "hr2").status_code == 403
    assert client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_me_requires_a_valid_token(client: TestClient) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Bearer junk"}).status_code == 401
    assert client.get("/api/v1/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {create_access_token(999999)}"})
    assert r.status_code == 401


def test_expired_access_token(client: TestClient, db: Session, monkeypatch) -> None:
    from app import security

    uid = db.scalar(select(User.id).where(User.username == "hr1"))
    assert uid is not None
    monkeypatch.setattr(security, "now_utc", lambda: now_utc() - timedelta(hours=1))
    old = create_access_token(uid)
    monkeypatch.undo()
    r = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {old}"})
    assert r.status_code == 401 and "expired" in r.json()["error"]["message"]


def test_refresh_rotates_and_detects_reuse(client: TestClient, db: Session) -> None:
    first = login(client).cookies.get("hr_refresh")
    assert first
    r = client.post("/api/v1/auth/refresh")  # the client sends the cookie
    assert r.status_code == 200, r.text
    second = r.cookies.get("hr_refresh")
    assert second and second != first
    assert decode_access_token(r.json()["accessToken"]) is not None
    # Using the first (already swapped) token again ends every session of this user.
    client.cookies.clear()
    client.cookies.set("hr_refresh", first, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401
    client.cookies.clear()
    client.cookies.set("hr_refresh", second, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").status_code == 401
    uid = db.scalar(select(User.id).where(User.username == "hr1"))
    live = db.scalars(select(RefreshToken).where(RefreshToken.user_id == uid, RefreshToken.revoked_at.is_(None))).all()
    assert live == []


def test_refresh_without_cookie_or_expired(client: TestClient, db: Session) -> None:
    assert client.post("/api/v1/auth/refresh").status_code == 401
    login(client)
    db.execute(update(RefreshToken).values(expires_at=now_utc() - timedelta(seconds=1)))
    assert client.post("/api/v1/auth/refresh").status_code == 401


def test_logout_revokes_refresh_token(client: TestClient) -> None:
    login(client)
    r = client.post("/api/v1/auth/logout")
    assert r.status_code == 204
    assert client.post("/api/v1/auth/refresh").status_code == 401
    assert client.post("/api/v1/auth/logout").status_code == 204  # harmless twice


def test_change_password(client: TestClient, as_hr: dict[str, str]) -> None:
    url = "/api/v1/auth/change-password"
    assert (
        client.post(url, json={"currentPassword": "bad", "newPassword": "brand-new-1"}, headers=as_hr).status_code
        == 422
    )
    assert (
        client.post(url, json={"currentPassword": "demo123", "newPassword": "short"}, headers=as_hr).status_code == 422
    )
    assert (
        client.post(url, json={"currentPassword": "demo123", "newPassword": "demo123"}, headers=as_hr).status_code
        == 422
    )
    assert (
        client.post(url, json={"currentPassword": "demo123", "newPassword": "brand-new-1"}, headers=as_hr).status_code
        == 204
    )
    assert login(client, "hr1", "demo123").status_code == 401
    assert login(client, "hr1", "brand-new-1").status_code == 200


def test_unknown_body_fields_are_refused(client: TestClient) -> None:
    r = client.post(LOGIN, json={"username": "hr1", "password": "demo123", "isAdmin": True})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid"
