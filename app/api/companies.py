from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import schemas as S
from app.db import get_db
from app.deps import Principal, current_principal, ensure_company, require, scope, visible_companies
from app.errors import conflict, not_found
from app.models import Company, Department
from app.services import audit

router = APIRouter(tags=["companies"])


@router.get("/companies", response_model=list[S.CompanyOut])
def list_companies(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> list[S.CompanyOut]:
    return [S.CompanyOut.model_validate(c) for c in visible_companies(db, p)]


@router.get("/departments", response_model=list[S.DepartmentOut])
def list_departments(
    company_id: int | None = Query(None),
    include_inactive: bool = Query(False),
    p: Principal = Depends(require("employees.view")),
    db: Session = Depends(get_db),
) -> list[S.DepartmentOut]:
    companies = scope(p, company_id)
    q = select(Department).order_by(Department.company_id, Department.name)
    if companies is not None:
        q = q.where(Department.company_id.in_(companies))
    if not include_inactive:
        q = q.where(Department.is_active.is_(True))
    return [S.DepartmentOut.model_validate(d) for d in db.scalars(q).unique()]


def _save(db: Session, what: str) -> None:
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise conflict(f"A department named '{what}' already exists in this company.") from exc


@router.post("/departments", response_model=S.DepartmentOut, status_code=201)
def create_department(
    body: S.DepartmentIn, p: Principal = Depends(require("employees.edit")), db: Session = Depends(get_db)
) -> S.DepartmentOut:
    ensure_company(p, body.company_id, "Company")
    if db.get(Company, body.company_id) is None:
        raise not_found("Company")
    d = Department(company_id=body.company_id, name=body.name, created_by=p.id, updated_by=p.id)
    db.add(d)
    _save(db, body.name)
    audit.record(db, p.id, "create", "department", d.id, d.company_id, after=audit.snap(d))
    db.commit()
    return S.DepartmentOut.model_validate(d)


@router.patch("/departments/{department_id}", response_model=S.DepartmentOut)
def update_department(
    department_id: int,
    body: S.DepartmentPatch,
    p: Principal = Depends(require("employees.edit")),
    db: Session = Depends(get_db),
) -> S.DepartmentOut:
    d = db.get(Department, department_id)
    if d is None:
        raise not_found("Department")
    ensure_company(p, d.company_id, "Department")
    before = audit.snap(d)
    changes = body.model_dump(exclude_unset=True, exclude_none=True)
    for k, v in changes.items():
        setattr(d, k, v)
    d.updated_by = p.id
    _save(db, d.name)
    audit.record(db, p.id, "update", "department", d.id, d.company_id, before=before, after=audit.snap(d))
    db.commit()
    return S.DepartmentOut.model_validate(d)
