from fastapi.testclient import TestClient
from sqlalchemy.orm import Session


def test_employee_ticket_history(client: TestClient, as_manager: dict[str, str], emp) -> None:
    t = client.get(f"/api/v1/employees/{emp('SPE166').id}/tickets", headers=as_manager).json()
    assert t["info"]["cycleYears"] == 1 and t["info"]["lastIssued"] == "2025-03-05"
    assert t["info"]["nextDue"] == "2026-03-05" and t["info"]["overdue"] is True
    assert [h["issuedOn"] for h in t["history"]] == ["2025-03-05", "2024-02-12", "2023-02-10"]
    assert t["history"][0]["scheduled"] == "2025-02-12" and t["history"][0]["early"] is False
    assert t["history"][0]["issuedBy"] == "HR Officer 2"


def test_issue_and_edit_ticket(client: TestClient, as_hr: dict[str, str], as_manager: dict[str, str], emp) -> None:
    e = emp("SPE166")
    url = f"/api/v1/employees/{e.id}/tickets"
    assert (
        client.post(url, json={"issuedOn": "2026-11-20", "destination": "Manila"}, headers=as_manager).status_code
        == 403
    )
    assert client.post(url, json={"issuedOn": "2026-12-20"}, headers=as_hr).status_code == 422  # future
    assert client.post(url, json={"issuedOn": "2020-01-01"}, headers=as_hr).status_code == 422  # before joining
    assert client.post(url, json={"issuedOn": "2025-03-05"}, headers=as_hr).status_code == 409  # same day twice
    r = client.post(
        url, json={"issuedOn": "2026-11-20", "destination": "Manila", "note": "Late, approved by GM"}, headers=as_hr
    )
    assert r.status_code == 201, r.text
    assert (
        r.json()["scheduled"] == "2026-03-05" and r.json()["early"] is False and r.json()["issuedBy"] == "HR Officer 1"
    )
    info = client.get(url, headers=as_hr).json()["info"]
    assert info["nextDue"] == "2027-11-20" and info["overdue"] is False and info["issuedCount"] == 4
    tid = r.json()["id"]
    edited = client.patch(f"/api/v1/tickets/{tid}", json={"destination": "Cebu", "note": None}, headers=as_hr)
    assert edited.status_code == 200 and edited.json()["destination"] == "Cebu" and edited.json()["note"] == ""
    assert client.patch(f"/api/v1/tickets/{tid}", json={"issuedOn": None}, headers=as_hr).status_code == 422
    assert client.patch(f"/api/v1/tickets/{tid}", json={"issuedOn": "2025-03-05"}, headers=as_hr).status_code == 409
    assert client.patch("/api/v1/tickets/999999", json={"destination": "x"}, headers=as_hr).status_code == 404


def test_not_eligible_employee(client: TestClient, as_hr: dict[str, str], emp) -> None:
    e = emp("SPE159")
    assert client.get(f"/api/v1/employees/{e.id}/tickets", headers=as_hr).json()["info"]["eligible"] is False
    r = client.post(f"/api/v1/employees/{e.id}/tickets", json={"issuedOn": "2026-11-01"}, headers=as_hr)
    assert r.status_code == 422 and "not eligible" in r.json()["error"]["message"]


def test_due_list_and_summary(client: TestClient, as_manager: dict[str, str], db: Session) -> None:
    due = client.get("/api/v1/tickets/due", headers=as_manager).json()
    assert due and due[0]["overdue"] is True
    assert "SPE166" in {d["empNo"] for d in due}
    assert all(d["overdue"] or d["daysLeft"] <= 60 for d in due)
    wide = client.get("/api/v1/tickets/due?within_days=730", headers=as_manager).json()
    assert len(wide) >= len(due)
    s = client.get("/api/v1/tickets/summary", headers=as_manager).json()
    assert s["eligible"] == 20 and s["overdue"] >= 1
    assert s["issuedThisYear"] == 2 and s["earlyThisYear"] == 2  # SPW009 and SPE156 were given tickets early in 2026


def test_ticket_list(client: TestClient, as_manager: dict[str, str], emp, db: Session) -> None:
    from tests.conftest import company_id

    all_ = client.get("/api/v1/tickets?size=500", headers=as_manager).json()
    assert all_["total"] == 13 and all_["items"][0]["issuedOn"] == "2026-10-05"
    mine = client.get(f"/api/v1/tickets?employee_id={emp('SPE166').id}", headers=as_manager).json()
    assert mine["total"] == 3
    trd = client.get(f"/api/v1/tickets?company_id={company_id(db, 'TRD')}&size=500", headers=as_manager).json()
    assert {t["employeeId"] for t in trd["items"]} <= {
        emp(n).id for n in ("SPE166", "SPW031", "SPW033", "SPE164", "SPE159", "SPE163", "SPE165")
    }
