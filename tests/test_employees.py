from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Department
from tests.conftest import company_id


def _dept(db: Session, code: str, name: str) -> int:
    d = db.scalar(select(Department.id).where(Department.company_id == company_id(db, code), Department.name == name))
    assert d is not None
    return d


def new_employee(db: Session, **over: object) -> dict[str, object]:
    body: dict[str, object] = {
        "companyId": company_id(db, "SPL"),
        "departmentId": _dept(db, "SPL", "Production"),
        "empNo": "SPW900",
        "firstName": "Test",
        "lastName": "Person",
        "category": "B",
        "joiningDate": "2024-05-01",
        "ticketEligible": True,
        "documents": [{"docType": "passport", "docNumber": "P1", "expiryDate": "2027-01-10"}],
    }
    body.update(over)
    return body


def test_list_paginates_filters_and_sorts(client: TestClient, as_hr: dict[str, str], db: Session) -> None:
    r = client.get("/api/v1/employees?size=5&page=2", headers=as_hr).json()
    assert r["total"] == 23 and r["page"] == 2 and len(r["items"]) == 5
    spl = client.get(f"/api/v1/employees?company_id={company_id(db, 'SPL')}&size=100", headers=as_hr).json()
    assert spl["total"] == 9
    b = client.get("/api/v1/employees?category=B&size=100", headers=as_hr).json()
    assert b["total"] == 12 and all(x["category"] == "B" for x in b["items"])
    q = client.get("/api/v1/employees?q=spw01", headers=as_hr).json()
    assert {x["empNo"] for x in q["items"]} == {"SPW010", "SPW011", "SPW013"}
    newest = client.get("/api/v1/employees?sort=-joining_date&size=1", headers=as_hr).json()["items"][0]
    assert newest["empNo"] == "SPE165"
    assert client.get("/api/v1/employees?size=1000", headers=as_hr).status_code == 422
    assert client.get("/api/v1/employees?sort=password", headers=as_hr).status_code == 422


def test_employee_shape(client: TestClient, as_hr: dict[str, str], emp) -> None:
    e = client.get(f"/api/v1/employees/{emp('SPW013').id}", headers=as_hr).json()
    assert (
        e["fullName"].startswith(e["firstName"])
        and e["company"]["code"] == "SPL"
        and e["department"]["name"] == "Production"
    )
    assert e["service"] == "15 years 2 months"
    docs = {d["docType"]: d for d in e["documents"]}
    assert docs["emirates_id"]["level"] == "soon" and docs["emirates_id"]["daysLeft"] == 31
    assert docs["passport"]["level"] == "ok"


def test_create_employee_with_documents(client: TestClient, as_hr: dict[str, str], db: Session) -> None:
    r = client.post("/api/v1/employees", json=new_employee(db), headers=as_hr)
    assert r.status_code == 201, r.text
    e = r.json()
    assert e["empNo"] == "SPW900" and e["status"] == "active" and e["documents"][0]["docNumber"] == "P1"
    assert db.scalar(
        select(AuditLog).where(
            AuditLog.entity == "employee", AuditLog.entity_id == e["id"], AuditLog.action == "create"
        )
    )


def test_create_rejects_bad_input(client: TestClient, as_hr: dict[str, str], db: Session) -> None:
    url = "/api/v1/employees"
    dup = client.post(url, json=new_employee(db, empNo="spw013"), headers=as_hr)
    assert dup.status_code == 409 and "already used" in dup.json()["error"]["message"]
    wrong_dept = client.post(url, json=new_employee(db, departmentId=_dept(db, "SSG", "Production")), headers=as_hr)
    assert wrong_dept.status_code == 422 and "department" in wrong_dept.json()["error"]["message"]
    assert client.post(url, json=new_employee(db, category="C"), headers=as_hr).status_code == 422
    assert client.post(url, json=new_employee(db, firstName=""), headers=as_hr).status_code == 422
    assert client.post(url, json=new_employee(db, email="not-an-email"), headers=as_hr).status_code == 422
    assert client.post(url, json=new_employee(db, empNo="bad no!"), headers=as_hr).status_code == 422
    left = client.post(url, json=new_employee(db, status="left"), headers=as_hr)
    assert left.status_code == 422 and "leaving date" in left.json()["error"]["message"]
    before = client.post(url, json=new_employee(db, leavingDate="2020-01-01"), headers=as_hr)
    assert before.status_code == 422
    twice = new_employee(db, documents=[{"docType": "passport", "expiryDate": "2030-01-01"}] * 2)
    assert client.post(url, json=twice, headers=as_hr).status_code == 422
    assert client.post(url, json=new_employee(db, companyId=99999), headers=as_hr).status_code in (404, 422)


def test_update_employee(client: TestClient, as_hr: dict[str, str], emp, db: Session) -> None:
    e = emp("SPE156")
    url = f"/api/v1/employees/{e.id}"
    r = client.patch(url, json={"designation": "Senior Accountant", "mobile": "+971500000000"}, headers=as_hr)
    assert r.status_code == 200 and r.json()["designation"] == "Senior Accountant"
    log = db.scalar(
        select(AuditLog).where(AuditLog.entity == "employee", AuditLog.entity_id == e.id, AuditLog.action == "update")
    )
    assert log is not None and log.after == {"designation": "Senior Accountant", "mobile": "+971500000000"}
    # move to another company: the department must move too
    ssg = company_id(db, "SSG")
    assert client.patch(url, json={"companyId": ssg}, headers=as_hr).status_code == 422
    moved = client.patch(url, json={"companyId": ssg, "departmentId": _dept(db, "SSG", "Quality")}, headers=as_hr)
    assert moved.status_code == 200 and moved.json()["company"]["code"] == "SSG"
    assert client.patch(url, json={"empNo": "SPW013"}, headers=as_hr).status_code == 409
    assert client.patch(url, json={"firstName": None}, headers=as_hr).status_code == 422
    left = client.patch(url, json={"status": "left", "leavingDate": "2026-11-30"}, headers=as_hr)
    assert left.status_code == 200 and left.json()["status"] == "left"
    assert client.get("/api/v1/employees/999999", headers=as_hr).status_code == 404


def test_search_returns_hr_view(client: TestClient, as_manager: dict[str, str]) -> None:
    hits = client.get("/api/v1/employees/search?q=SPW013", headers=as_manager).json()
    assert hits[0]["empNo"] == "SPW013"
    h = hits[0]
    assert h["department"] == "Production" and h["annualRemaining"] == 14.0
    assert h["lastLeave"]["leaveType"] == "annual" and h["lastLeave"]["days"] == 60
    assert h["ticket"]["nextDue"] == "2027-11-03"
    by_name = client.get("/api/v1/employees/search?q=srini", headers=as_manager).json()
    assert {h["empNo"] for h in by_name} == {"SPE156", "SPW008"}
    typo = client.get("/api/v1/employees/search?q=srinivasan palanipilai", headers=as_manager).json()
    assert typo and typo[0]["empNo"] == "SPE156"
    assert client.get("/api/v1/employees/search?q=", headers=as_manager).status_code == 422


def test_summary_balances_and_stats(client: TestClient, as_manager: dict[str, str], emp) -> None:
    eid = emp("SPW013").id
    s = client.get(f"/api/v1/employees/{eid}/summary", headers=as_manager).json()
    annual = s["balances"][0]
    assert annual == {
        "leaveType": "annual",
        "entitled": 60.0,
        "used": 46,
        "pending": 0,
        "adjusted": 0.0,
        "remaining": 14.0,
        "period": "2025-09-05 to 2027-09-04",
        "periodStart": "2025-09-05",
        "periodEnd": "2027-09-04",
    }
    assert s["ticket"]["nextDue"] == "2027-11-03" and s["nextLeavePlan"] == "2027-10-20"
    assert client.get(f"/api/v1/employees/{eid}/balances", headers=as_manager).json()[0] == annual
    st = client.get(f"/api/v1/employees/{eid}/stats", headers=as_manager).json()
    assert st["byYear"][0]["year"] == 2011 and st["byYear"][-1]["year"] == 2026
    assert len(st["last12Months"]) == 12 and st["last12Months"][-1]["month"] == "2026-11"
    assert st["byType"]["annual"] > 0 and st["byStatus"]["approved"] > 0


def test_departments(client: TestClient, as_hr: dict[str, str], as_manager: dict[str, str], db: Session) -> None:
    spl = company_id(db, "SPL")
    deps = client.get(f"/api/v1/departments?company_id={spl}", headers=as_manager).json()
    assert {d["name"] for d in deps} >= {"Production", "Maintenance"}
    r = client.post("/api/v1/departments", json={"companyId": spl, "name": "IT"}, headers=as_hr)
    assert r.status_code == 201
    assert client.post("/api/v1/departments", json={"companyId": spl, "name": "IT"}, headers=as_hr).status_code == 409
    did = r.json()["id"]
    off = client.patch(f"/api/v1/departments/{did}", json={"isActive": False}, headers=as_hr)
    assert off.status_code == 200 and off.json()["isActive"] is False
    assert did not in [d["id"] for d in client.get("/api/v1/departments", headers=as_hr).json()]
    assert did in [d["id"] for d in client.get("/api/v1/departments?include_inactive=true", headers=as_hr).json()]
    assert client.patch("/api/v1/departments/999999", json={"name": "x"}, headers=as_hr).status_code == 404
    assert (
        client.post("/api/v1/departments", json={"companyId": spl, "name": "IT2"}, headers=as_manager).status_code
        == 403
    )
