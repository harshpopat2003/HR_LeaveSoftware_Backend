from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, DocumentRenewal, ReminderLog


def test_employee_documents_and_correction(client: TestClient, as_hr: dict[str, str], emp, db: Session) -> None:
    e = emp("SPE156")
    base = f"/api/v1/employees/{e.id}/documents"
    docs = client.get(base, headers=as_hr).json()
    assert [d["docType"] for d in docs] == ["passport", "emirates_id"]
    r = client.put(f"{base}/passport", json={"docNumber": "X999", "expiryDate": "2029-02-01"}, headers=as_hr)
    assert r.status_code == 200 and r.json()["docNumber"] == "X999"
    # add a visit visa, then remove it
    add = client.put(f"{base}/visit_visa", json={"docNumber": "V1", "expiryDate": "2026-12-20"}, headers=as_hr)
    assert add.status_code == 200 and add.json()["level"] == "soon"
    assert client.put(f"{base}/visit_visa", json={"expiryDate": None}, headers=as_hr).status_code == 204
    assert client.put(f"{base}/visit_visa", json={"expiryDate": None}, headers=as_hr).status_code == 204
    assert [d["docType"] for d in client.get(base, headers=as_hr).json()] == ["passport", "emirates_id"]
    assert client.put(f"{base}/driving_licence", json={"expiryDate": "2030-01-01"}, headers=as_hr).status_code == 422
    actions = db.scalars(select(AuditLog.action).where(AuditLog.entity == "employee_document")).all()
    assert {"update", "create", "delete"} <= set(actions)


def test_renew_document(client: TestClient, as_hr: dict[str, str], emp, db: Session) -> None:
    e = emp("SPW009")  # passport expired on 2026-11-20
    url = f"/api/v1/employees/{e.id}/documents/passport/renew"
    assert client.post(url, json={"newExpiry": "2026-11-01"}, headers=as_hr).status_code == 422
    assert (
        client.post(url, json={"newExpiry": "2036-11-01", "renewedOn": "2027-01-01"}, headers=as_hr).status_code == 422
    )
    r = client.post(url, json={"newExpiry": "2036-11-19", "newNumber": "N123", "note": "New passport"}, headers=as_hr)
    assert r.status_code == 201, r.text
    ren = r.json()
    assert ren["oldExpiry"] == "2026-11-20" and ren["newExpiry"] == "2036-11-19" and ren["newNumber"] == "N123"
    assert ren["renewedOn"] == "2026-11-27" and ren["renewedBy"] == "HR Officer 1"
    docs = {d["docType"]: d for d in client.get(f"/api/v1/employees/{e.id}/documents", headers=as_hr).json()}
    assert docs["passport"]["level"] == "ok" and docs["passport"]["docNumber"] == "N123"
    assert db.scalar(select(DocumentRenewal).where(DocumentRenewal.id == ren["id"])) is not None
    missing = client.post(
        f"/api/v1/employees/{e.id}/documents/visit_visa/renew", json={"newExpiry": "2030-01-01"}, headers=as_hr
    )
    assert missing.status_code == 404


def test_document_lists(client: TestClient, as_manager: dict[str, str]) -> None:
    alerts = client.get("/api/v1/documents/alerts", headers=as_manager).json()
    assert [a["level"] for a in alerts][:2] == ["expired", "expired"]
    assert {(a["empNo"], a["docType"]) for a in alerts if a["level"] == "expired"} == {
        ("SPW021", "emirates_id"),
        ("SPW009", "passport"),
    }
    days = [a["daysLeft"] for a in alerts]
    assert days == sorted(days)
    urgent = client.get("/api/v1/documents?level=urgent", headers=as_manager).json()
    assert {x["empNo"] for x in urgent["items"]} == {"SPW031", "SPW004"}
    visas = client.get("/api/v1/documents?doc_type=visit_visa", headers=as_manager).json()
    assert visas["total"] == 2
    paged = client.get("/api/v1/documents?size=5&page=2", headers=as_manager).json()
    assert paged["total"] == 46 and len(paged["items"]) == 5
    summary = {s["docType"]: s for s in client.get("/api/v1/documents/summary", headers=as_manager).json()}
    assert summary["passport"]["expired"] == 1 and summary["passport"]["urgent"] == 1
    assert summary["emirates_id"]["missing"] == 2 and summary["visit_visa"]["missing"] == 0
    assert sum(summary["passport"][k] for k in ("expired", "urgent", "soon", "ok")) == 23


def test_renewal_history(client: TestClient, as_manager: dict[str, str], emp) -> None:
    all_ = client.get("/api/v1/documents/renewals", headers=as_manager).json()
    assert all_["total"] == 3 and all_["items"][0]["renewedOn"] == "2025-08-20"
    mine = client.get(f"/api/v1/documents/renewals?employee_id={emp('SPW011').id}", headers=as_manager).json()
    assert (
        mine["total"] == 1
        and mine["items"][0]["newNumber"] == "P4471902"
        and mine["items"][0]["renewedBy"] == "HR Officer 2"
    )
    assert client.get("/api/v1/documents/renewals?employee_id=999999", headers=as_manager).status_code == 404


def test_daily_reminder_once_a_day(client: TestClient, as_manager: dict[str, str], db: Session) -> None:
    r = client.get("/api/v1/reminders/today", headers=as_manager).json()
    assert r["date"] == "2026-11-27" and r["seen"] is False
    assert {d["level"] for d in r["documents"]} == {"expired", "urgent"}
    assert all(t["overdue"] or t["daysLeft"] <= 30 for t in r["tickets"])
    assert client.post("/api/v1/reminders/today/seen", headers=as_manager).status_code == 204
    assert client.post("/api/v1/reminders/today/seen", headers=as_manager).status_code == 204  # twice is fine
    assert client.get("/api/v1/reminders/today", headers=as_manager).json()["seen"] is True
    assert len(db.scalars(select(ReminderLog)).all()) == 1


def test_manager_cannot_edit_documents(client: TestClient, as_manager: dict[str, str], emp) -> None:
    e = emp("SPW009")
    r = client.post(
        f"/api/v1/employees/{e.id}/documents/passport/renew", json={"newExpiry": "2036-01-01"}, headers=as_manager
    )
    assert r.status_code == 403
