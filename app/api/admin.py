"""Users, roles and permissions, rules and the audit trail."""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import schemas as S
from app.db import get_db
from app.deps import PageParams, Principal, current_principal, ensure_company, paging, require
from app.errors import conflict, forbidden, invalid, not_found
from app.models import AuditLog, Company, Permission, Role, RolePermission, RuleSet, User, UserCompanyAccess
from app.rules.leave_rules import RulesConfig
from app.security import hash_password
from app.services import audit, present
from app.services import rules as rules_svc
from app.services.auth import revoke_all

router = APIRouter(tags=["admin"])

ADMIN_ROLE = "administrator"


# ---------------------------------------------------------------- users
def _get_user(db: Session, user_id: int) -> User:
    u = db.get(User, user_id)
    if u is None:
        raise not_found("User")
    return u


def _role(db: Session, role_id: int) -> Role:
    r = db.get(Role, role_id)
    if r is None:
        raise invalid("Unknown role.", {"field": "roleId"})
    return r


def _companies(db: Session, ids: list[int]) -> list[Company]:
    ids = sorted(set(ids))
    rows = list(db.scalars(select(Company).where(Company.id.in_(ids)))) if ids else []
    if len(rows) != len(ids):
        raise invalid("Unknown company.", {"field": "companyIds"})
    return rows


def _active_admins(db: Session, exclude: int | None = None) -> int:
    q = select(func.count()).select_from(User).join(Role).where(Role.key == ADMIN_ROLE, User.is_active.is_(True))
    if exclude is not None:
        q = q.where(User.id != exclude)
    return db.scalar(q) or 0


def _save_user(db: Session) -> None:
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise conflict("This username or email is already used.") from exc


@router.get("/users", response_model=list[S.UserOut])
def list_users(_: Principal = Depends(require("users.manage")), db: Session = Depends(get_db)) -> list[S.UserOut]:
    return [present.user_out(u) for u in db.scalars(select(User).order_by(User.id)).unique()]


@router.post("/users", response_model=S.UserOut, status_code=201)
def create_user(
    body: S.UserIn, p: Principal = Depends(require("users.manage")), db: Session = Depends(get_db)
) -> S.UserOut:
    role = _role(db, body.role_id)
    u = User(
        username=body.username,
        full_name=body.full_name,
        email=body.email,
        password_hash=hash_password(body.password),
        role_id=role.id,
    )
    u.companies = _companies(db, body.company_ids)
    db.add(u)
    _save_user(db)
    audit.record(db, p.id, "create", "user", u.id, after=audit.snap(u) | {"companies": body.company_ids})
    db.commit()
    db.refresh(u)
    return present.user_out(u)


@router.get("/users/{user_id}", response_model=S.UserOut)
def get_user(user_id: int, _: Principal = Depends(require("users.manage")), db: Session = Depends(get_db)) -> S.UserOut:
    return present.user_out(_get_user(db, user_id))


@router.patch("/users/{user_id}", response_model=S.UserOut)
def update_user(
    user_id: int, body: S.UserPatch, p: Principal = Depends(require("users.manage")), db: Session = Depends(get_db)
) -> S.UserOut:
    u = _get_user(db, user_id)
    before = audit.snap(u)
    changes = body.model_dump(exclude_unset=True)
    losing_admin = (
        changes.get("is_active") is False or ("role_id" in changes and _role(db, changes["role_id"]).key != ADMIN_ROLE)
    ) and u.role.key == ADMIN_ROLE
    if losing_admin and _active_admins(db, exclude=u.id) == 0:
        raise conflict("At least one active System Administrator must remain.")
    if u.id == p.id and changes.get("is_active") is False:
        raise conflict("You cannot switch off your own account.")
    if "full_name" in changes and changes["full_name"]:
        u.full_name = changes["full_name"]
    if "email" in changes:
        u.email = changes["email"]
    if changes.get("role_id"):
        u.role_id = _role(db, changes["role_id"]).id
    if changes.get("is_active") is not None:
        u.is_active = changes["is_active"]
        if not u.is_active:
            revoke_all(db, u.id)
    if changes.get("unlock"):
        u.locked_until = None
        u.failed_logins = 0
    _save_user(db)
    db.refresh(u)
    audit.record(db, p.id, "update", "user", u.id, before=before, after=audit.snap(u))
    db.commit()
    return present.user_out(u)


@router.post("/users/{user_id}/reset-password", status_code=204)
def reset_password(
    user_id: int,
    body: S.ResetPasswordIn,
    p: Principal = Depends(require("users.manage")),
    db: Session = Depends(get_db),
) -> Response:
    u = _get_user(db, user_id)
    u.password_hash = hash_password(body.new_password)
    u.locked_until = None
    u.failed_logins = 0
    revoke_all(db, u.id)
    audit.record(db, p.id, "reset_password", "user", u.id)
    db.commit()
    return Response(status_code=204)


@router.put("/users/{user_id}/companies", response_model=S.UserOut)
def set_user_companies(
    user_id: int, body: S.CompanyIdsIn, p: Principal = Depends(require("users.manage")), db: Session = Depends(get_db)
) -> S.UserOut:
    """Limit a user to some companies. An empty list means every company."""
    u = _get_user(db, user_id)
    before = [c.id for c in u.companies]
    comps = _companies(db, body.company_ids)
    db.execute(delete(UserCompanyAccess).where(UserCompanyAccess.user_id == u.id))
    db.add_all(UserCompanyAccess(user_id=u.id, company_id=c.id) for c in comps)
    db.flush()
    db.expire(u, ["companies"])
    audit.record(
        db,
        p.id,
        "set_companies",
        "user",
        u.id,
        before={"companies": before},
        after={"companies": [c.id for c in comps]},
    )
    db.commit()
    return present.user_out(u)


# ---------------------------------------------------------------- roles and permissions
def _role_out(db: Session, r: Role) -> S.RoleOut:
    count = db.scalar(select(func.count()).select_from(User).where(User.role_id == r.id)) or 0
    return S.RoleOut(
        id=r.id,
        key=r.key,
        name=r.name,
        description=r.description,
        is_system=r.is_system,
        permissions=[x.key for x in r.permissions],
        user_count=count,
    )


def _permissions(db: Session, keys: list[str]) -> list[Permission]:
    keys = sorted(set(keys))
    rows = list(db.scalars(select(Permission).where(Permission.key.in_(keys)))) if keys else []
    unknown = set(keys) - {r.key for r in rows}
    if unknown:
        raise invalid("Unknown permission.", {"field": "permissions", "unknown": sorted(unknown)})
    return rows


def _set_permissions(db: Session, r: Role, perms: list[Permission]) -> None:
    db.execute(delete(RolePermission).where(RolePermission.role_id == r.id))
    db.add_all(RolePermission(role_id=r.id, permission_id=x.id) for x in perms)
    db.flush()
    db.expire(r, ["permissions"])


@router.get("/permissions", response_model=list[S.PermissionOut])
def list_permissions(
    _: Principal = Depends(require("roles.manage")), db: Session = Depends(get_db)
) -> list[S.PermissionOut]:
    return [S.PermissionOut.model_validate(x) for x in db.scalars(select(Permission).order_by(Permission.id))]


@router.get("/roles", response_model=list[S.RoleOut])
def list_roles(_: Principal = Depends(require("roles.manage")), db: Session = Depends(get_db)) -> list[S.RoleOut]:
    return [_role_out(db, r) for r in db.scalars(select(Role).order_by(Role.id)).unique()]


@router.post("/roles", response_model=S.RoleOut, status_code=201)
def create_role(
    body: S.RoleIn, p: Principal = Depends(require("roles.manage")), db: Session = Depends(get_db)
) -> S.RoleOut:
    perms = _permissions(db, body.permissions)
    r = Role(key=body.key, name=body.name, description=body.description, is_system=False)
    db.add(r)
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise conflict("A role with this key or name already exists.") from exc
    _set_permissions(db, r, perms)
    audit.record(db, p.id, "create", "role", r.id, after=audit.snap(r) | {"permissions": [x.key for x in perms]})
    db.commit()
    return _role_out(db, r)


@router.patch("/roles/{role_id}", response_model=S.RoleOut)
def update_role(
    role_id: int, body: S.RolePatch, p: Principal = Depends(require("roles.manage")), db: Session = Depends(get_db)
) -> S.RoleOut:
    r = db.get(Role, role_id)
    if r is None:
        raise not_found("Role")
    if r.key == ADMIN_ROLE and body.permissions is not None:
        raise conflict("The System Administrator role always has every permission.")
    before = audit.snap(r) | {"permissions": [x.key for x in r.permissions]}
    if body.name:
        r.name = body.name
    if body.description is not None:
        r.description = body.description
    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise conflict("A role with this name already exists.") from exc
    if body.permissions is not None:
        _set_permissions(db, r, _permissions(db, body.permissions))
    audit.record(
        db,
        p.id,
        "update",
        "role",
        r.id,
        before=before,
        after=audit.snap(r) | {"permissions": [x.key for x in r.permissions]},
    )
    db.commit()
    return _role_out(db, r)


@router.delete("/roles/{role_id}", status_code=204)
def delete_role(
    role_id: int, p: Principal = Depends(require("roles.manage")), db: Session = Depends(get_db)
) -> Response:
    r = db.get(Role, role_id)
    if r is None:
        raise not_found("Role")
    if r.is_system:
        raise conflict("Built-in roles cannot be deleted.")
    if db.scalar(select(func.count()).select_from(User).where(User.role_id == r.id)):
        raise conflict("Move the users of this role to another role first.")
    audit.record(db, p.id, "delete", "role", r.id, before=audit.snap(r))
    db.delete(r)
    db.commit()
    return Response(status_code=204)


# ---------------------------------------------------------------- rules
def _rules_out(r: RuleSet | None, config: RulesConfig, company_id: int | None, inherited: bool) -> S.RulesOut:
    return S.RulesOut(
        company_id=company_id,
        version=r.version if r else 0,
        inherited=inherited,
        config=config,
        created_at=r.created_at if r else None,
    )


@router.get("/rules", response_model=S.RulesOut)
def get_rules(
    company_id: int | None = None, p: Principal = Depends(current_principal), db: Session = Depends(get_db)
) -> S.RulesOut:
    """Rules in force for a company (its own override, else the group set). Without company_id: the group set."""
    if company_id is not None:
        ensure_company(p, company_id, "Company")
    config, row, inherited = rules_svc.effective(db, company_id)
    return _rules_out(row, config, company_id, inherited)


@router.put("/rules", response_model=S.RulesOut)
def put_rules(
    body: S.RulesIn, p: Principal = Depends(require("rules.edit")), db: Session = Depends(get_db)
) -> S.RulesOut:
    if body.company_id is not None:
        if db.get(Company, body.company_id) is None:
            raise not_found("Company")
        ensure_company(p, body.company_id, "Company")
    elif p.company_ids is not None:
        raise forbidden("Only users with access to every company can change the group rules.")
    before, _, _ = rules_svc.effective(db, body.company_id)
    row = rules_svc.save(db, body.company_id, body.config, p.id)
    audit.record(
        db,
        p.id,
        "update",
        "rule_set",
        row.id,
        body.company_id,
        before=before.model_dump(mode="json"),
        after=body.config.model_dump(mode="json"),
    )
    db.commit()
    return _rules_out(row, body.config, body.company_id, False)


@router.get("/rules/history", response_model=list[S.RulesOut])
def rules_history(
    company_id: int | None = None, p: Principal = Depends(require("rules.edit")), db: Session = Depends(get_db)
) -> list[S.RulesOut]:
    if company_id is not None:
        ensure_company(p, company_id, "Company")
    return [
        _rules_out(r, RulesConfig.model_validate(r.config), company_id, False)
        for r in rules_svc.history(db, company_id)
    ]


# ---------------------------------------------------------------- audit
@router.get("/audit", response_model=S.Page[S.AuditOut])
def list_audit(
    entity: str | None = Query(None, max_length=40),
    entity_id: int | None = None,
    user_id: int | None = None,
    since: datetime | None = None,
    pp: PageParams = Depends(paging),
    _: Principal = Depends(require("users.manage")),
    db: Session = Depends(get_db),
) -> S.Page[S.AuditOut]:
    q = select(AuditLog, User.username).outerjoin(User, User.id == AuditLog.user_id)
    if entity:
        q = q.where(AuditLog.entity == entity)
    if entity_id is not None:
        q = q.where(AuditLog.entity_id == entity_id)
    if user_id is not None:
        q = q.where(AuditLog.user_id == user_id)
    if since is not None:
        q = q.where(AuditLog.at >= since)
    total = db.scalar(select(func.count()).select_from(q.order_by(None).subquery())) or 0
    rows = db.execute(q.order_by(AuditLog.at.desc(), AuditLog.id.desc()).offset(pp.offset).limit(pp.size)).all()
    items = [
        S.AuditOut(
            id=a.id,
            at=a.at,
            user_id=a.user_id,
            username=name,
            action=a.action,
            entity=a.entity,
            entity_id=a.entity_id,
            company_id=a.company_id,
            before=a.before,
            after=a.after,
            ip=a.ip,
        )
        for a, name in rows
    ]
    return S.Page(items=items, total=total, page=pp.page, size=pp.size)
