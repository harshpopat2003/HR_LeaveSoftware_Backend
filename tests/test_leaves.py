from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AuditLog, LeaveRequest, LeaveType


def _leave(db: Session, emp_no: str, status: str = "pending") -> LeaveRequest:
    from app.models import Employee

    lv = db.scalar(
        select(LeaveRequest).join(Employee).where(Employee.emp_no == emp_no, LeaveRequest.status == status).limit(1)
    )
    assert lv is not None
    return lv


def test_preview(client: TestClient, as_hr: dict[str, str], emp) -> None:
    e = emp("SPE156")
    ok = client.post(
        "/api/v1/leaves/preview",
        json={"employeeId": e.id, "leaveType": "annual", "startDate": "2027-01-04", "endDate": "2027-01-08"},
        headers=as_hr,
    )
    assert ok.json() == {"ok": True, "errors": [], "warnings": [], "days": 5}
    long = client.post(
        "/api/v1/leaves/preview",
        json={"employeeId": e.id, "leaveType": "emergency", "startDate": "2027-01-04", "endDate": "2027-01-20"},
        headers=as_hr,
    )
    assert long.json()["ok"] is True and "go negative" in long.json()["warnings"][0]
    bad = client.post(
        "/api/v1/leaves/preview",
        json={"employeeId": e.id, "leaveType": "annual", "startDate": "2027-01-08", "endDate": "2027-01-04"},
        headers=as_hr,
    )
    assert bad.json()["ok"] is False
    unknown = client.post(
        "/api/v1/leaves/preview",
        json={"employeeId": e.id, "leaveType": "holiday", "startDate": "2027-01-04", "endDate": "2027-01-04"},
        headers=as_hr,
    )
    assert unknown.status_code == 422


def test_create_edit_approve_flow(
    client: TestClient, as_hr: dict[str, str], as_manager: dict[str, str], emp, db: Session
) -> None:
    e = emp("SPE156")
    r = client.post(
        "/api/v1/leaves",
        json={
            "employeeId": e.id,
            "leaveType": "annual",
            "startDate": "2027-01-04",
            "endDate": "2027-01-08",
            "reason": "Family",
        },
        headers=as_hr,
    )
    assert r.status_code == 201, r.text
    lv = r.json()["leave"]
    assert (
        lv["status"] == "pending"
        and lv["days"] == 5
        and lv["leaveTypeName"] == "Annual leave"
        and lv["empNo"] == "SPE156"
    )
    url = f"/api/v1/leaves/{lv['id']}"
    edited = client.patch(url, json={"endDate": "2027-01-10"}, headers=as_hr)
    assert edited.status_code == 200 and edited.json()["leave"]["days"] == 7
    assert client.patch(url, json={"startDate": None}, headers=as_hr).status_code == 422
    # balance shows it as pending
    bal = client.get(f"/api/v1/employees/{e.id}/balances", headers=as_hr).json()
    assert bal[0]["pending"] == 0  # 2027 leave does not touch the 2026 period
    approved = client.post(f"{url}/approve", json={"note": "OK"}, headers=as_manager)
    assert approved.status_code == 200
    body = approved.json()
    assert (
        body["status"] == "approved"
        and body["decidedBy"] == "Operations Manager"
        and body["decidedAt"]
        and body["decisionNote"] == "OK"
    )
    assert client.post(f"{url}/approve", headers=as_manager).status_code == 409
    assert client.post(f"{url}/reject", json={"note": "x"}, headers=as_manager).status_code == 409
    assert client.patch(url, json={"reason": "x"}, headers=as_hr).status_code == 409
    cancelled = client.post(f"{url}/cancel", json={"note": "Plans changed"}, headers=as_hr)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert client.post(f"{url}/cancel", headers=as_hr).status_code == 409
    actions = db.scalars(
        select(AuditLog.action)
        .where(AuditLog.entity == "leave_request", AuditLog.entity_id == lv["id"])
        .order_by(AuditLog.id)
    ).all()
    assert actions == ["create", "update", "approve", "cancel"]


def test_reject_needs_a_note(client: TestClient, as_manager: dict[str, str], db: Session) -> None:
    lv = _leave(db, "SPE160")
    assert client.post(f"/api/v1/leaves/{lv.id}/reject", json={"note": ""}, headers=as_manager).status_code == 422
    assert client.post(f"/api/v1/leaves/{lv.id}/reject", headers=as_manager).status_code == 422
    r = client.post(f"/api/v1/leaves/{lv.id}/reject", json={"note": "Busy period"}, headers=as_manager)
    assert r.status_code == 200 and r.json()["status"] == "rejected"


def test_overlap_is_refused(client: TestClient, as_hr: dict[str, str], emp) -> None:
    e = emp("SPW033")  # pending annual 2026-12-15 to 2027-02-12
    r = client.post(
        "/api/v1/leaves",
        json={"employeeId": e.id, "leaveType": "emergency", "startDate": "2027-01-01", "endDate": "2027-01-02"},
        headers=as_hr,
    )
    assert r.status_code == 422 and "Overlaps" in r.json()["error"]["message"]
    before_joining = client.post(
        "/api/v1/leaves",
        json={"employeeId": e.id, "leaveType": "other", "startDate": "2010-01-01", "endDate": "2010-01-02"},
        headers=as_hr,
    )
    assert before_joining.status_code == 422


def test_database_also_blocks_overlap(db: Session, emp) -> None:
    """Even if two requests slip past the check at the same moment, the exclusion constraint stops the second."""
    e = emp("SPE156")
    lt = db.scalar(select(LeaveType).where(LeaveType.code == "annual"))
    assert lt is not None
    db.add(
        LeaveRequest(
            employee_id=e.id,
            leave_type_id=lt.id,
            start_date=date(2027, 3, 1),
            end_date=date(2027, 3, 5),
            days=5,
            status="approved",
        )
    )
    db.flush()
    db.add(
        LeaveRequest(
            employee_id=e.id,
            leave_type_id=lt.id,
            start_date=date(2027, 3, 5),
            end_date=date(2027, 3, 6),
            days=2,
            status="pending",
        )
    )
    with pytest.raises(IntegrityError) as err:
        db.flush()
    assert "ex_leave_requests_no_overlap" in str(err.value)
    db.rollback()


def test_rejected_leave_does_not_block_dates(db: Session, emp) -> None:
    e = emp("SPE157")
    lt = db.scalar(select(LeaveType).where(LeaveType.code == "annual"))
    assert lt is not None
    db.add(
        LeaveRequest(
            employee_id=e.id,
            leave_type_id=lt.id,
            start_date=date(2027, 3, 1),
            end_date=date(2027, 3, 5),
            days=5,
            status="rejected",
        )
    )
    db.add(
        LeaveRequest(
            employee_id=e.id,
            leave_type_id=lt.id,
            start_date=date(2027, 3, 1),
            end_date=date(2027, 3, 5),
            days=5,
            status="pending",
        )
    )
    db.flush()


def test_lists_and_filters(client: TestClient, as_manager: dict[str, str], emp) -> None:
    all_ = client.get("/api/v1/leaves?size=500", headers=as_manager).json()
    assert all_["total"] == 69
    starts = [x["startDate"] for x in all_["items"]]
    assert starts == sorted(starts, reverse=True)
    pending = client.get("/api/v1/leaves?status=pending", headers=as_manager).json()
    assert pending["total"] == 4
    em = client.get("/api/v1/leaves?leave_type=emergency&size=100", headers=as_manager).json()
    assert em["total"] == 7 and {x["leaveType"] for x in em["items"]} == {"emergency"}
    dec = client.get("/api/v1/leaves?from=2026-12-01&to=2026-12-31", headers=as_manager).json()
    assert (
        all(x["startDate"] <= "2026-12-31" and x["endDate"] >= "2026-12-01" for x in dec["items"]) and dec["total"] >= 4
    )
    e = emp("SPW013")
    mine = client.get(f"/api/v1/employees/{e.id}/leaves", headers=as_manager).json()
    assert mine["total"] > 0 and {x["empNo"] for x in mine["items"]} == {"SPW013"}
    one = client.get(f"/api/v1/leaves/{mine['items'][0]['id']}", headers=as_manager)
    assert one.status_code == 200
    assert client.get("/api/v1/leaves/999999", headers=as_manager).status_code == 404
    assert client.get("/api/v1/leaves?status=unknown", headers=as_manager).status_code == 422


def test_approvals_queue(client: TestClient, as_manager: dict[str, str]) -> None:
    rows = client.get("/api/v1/approvals", headers=as_manager).json()
    assert [r["leave"]["empNo"] for r in rows] == ["SPW022", "SPE160", "SPW033", "SPE166"]
    assert rows[0]["balance"]["leaveType"] == "emergency"


def test_adjustments_change_the_balance(
    client: TestClient, as_hr: dict[str, str], as_manager: dict[str, str], emp
) -> None:
    e = emp("SPW013")
    url = f"/api/v1/employees/{e.id}/adjustments"
    assert (
        client.post(
            url, json={"leaveType": "annual", "days": 0, "effectiveDate": "2026-01-01", "reason": "x"}, headers=as_hr
        ).status_code
        == 422
    )
    assert (
        client.post(
            url, json={"leaveType": "annual", "days": 1.3, "effectiveDate": "2026-01-01", "reason": "x"}, headers=as_hr
        ).status_code
        == 422
    )
    assert (
        client.post(
            url, json={"leaveType": "annual", "days": 2, "effectiveDate": "2026-01-01", "reason": ""}, headers=as_hr
        ).status_code
        == 422
    )
    assert (
        client.post(
            url, json={"leaveType": "annual", "days": 2, "effectiveDate": "2001-01-01", "reason": "x"}, headers=as_hr
        ).status_code
        == 422
    )
    r = client.post(
        url,
        json={"leaveType": "annual", "days": 4.5, "effectiveDate": "2026-01-01", "reason": "Carried over by approval"},
        headers=as_hr,
    )
    assert r.status_code == 201 and r.json()["days"] == 4.5
    assert (
        client.post(
            url,
            json={"leaveType": "annual", "days": 1, "effectiveDate": "2026-01-01", "reason": "x"},
            headers=as_manager,
        ).status_code
        == 403
    )
    bal = client.get(f"/api/v1/employees/{e.id}/balances", headers=as_hr).json()[0]
    assert bal["adjusted"] == 4.5 and bal["remaining"] == 18.5
    assert [a["reason"] for a in client.get(url, headers=as_manager).json()] == ["Carried over by approval"]


def test_leave_types(client: TestClient, as_admin: dict[str, str], as_hr: dict[str, str], db: Session) -> None:
    assert [t["code"] for t in client.get("/api/v1/leave-types", headers=as_hr).json()] == [
        "annual",
        "emergency",
        "other",
    ]
    r = client.post("/api/v1/leave-types", json={"code": "study", "name": "Study leave"}, headers=as_admin)
    assert r.status_code == 201
    assert (
        client.post("/api/v1/leave-types", json={"code": "study", "name": "Again"}, headers=as_admin).status_code == 409
    )
    assert (
        client.post("/api/v1/leave-types", json={"code": "Bad Code", "name": "x"}, headers=as_admin).status_code == 422
    )
    tid = r.json()["id"]
    off = client.patch(f"/api/v1/leave-types/{tid}", json={"isActive": False}, headers=as_admin)
    assert off.status_code == 200 and off.json()["isActive"] is False
    assert "study" not in [t["code"] for t in client.get("/api/v1/leave-types", headers=as_hr).json()]
    assert "study" in [t["code"] for t in client.get("/api/v1/leave-types?include_inactive=true", headers=as_hr).json()]
    annual = db.scalar(select(LeaveType.id).where(LeaveType.code == "annual"))
    assert client.patch(f"/api/v1/leave-types/{annual}", json={"isActive": False}, headers=as_admin).status_code == 409
    assert client.patch("/api/v1/leave-types/99999", json={"name": "x"}, headers=as_admin).status_code == 404


def test_leave_for_employee_who_left(client: TestClient, as_hr: dict[str, str], emp) -> None:
    e = emp("SPE157")
    client.patch(f"/api/v1/employees/{e.id}", json={"status": "left", "leavingDate": "2026-12-31"}, headers=as_hr)
    r = client.post(
        "/api/v1/leaves/preview",
        json={"employeeId": e.id, "leaveType": "annual", "startDate": "2026-12-30", "endDate": "2027-01-02"},
        headers=as_hr,
    )
    body = r.json()
    assert body["ok"] is False and "leaving date" in body["errors"][0] and "marked left" in body["warnings"][-1]


def test_race_between_two_requests_returns_409(client: TestClient, as_hr: dict[str, str], emp, monkeypatch) -> None:
    """If the overlap check is passed by two requests at once, the database constraint answers with 409."""
    from app.rules import leave_rules as R
    from app.services import leaves as svc

    e = emp("SPW033")  # pending annual 2026-12-15 to 2027-02-12
    monkeypatch.setattr(svc, "validate", lambda *a, **k: R.Validation(True, [], [], 2))
    body = {"employeeId": e.id, "leaveType": "other", "startDate": "2027-01-01", "endDate": "2027-01-02"}
    r = client.post("/api/v1/leaves", json=body, headers=as_hr)
    assert r.status_code == 409 and "overlaps" in r.json()["error"]["message"]
