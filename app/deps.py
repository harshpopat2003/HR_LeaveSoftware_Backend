"""Request dependencies: the signed-in user, permission checks and the company filter."""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.errors import AppError, forbidden
from app.models import Company, User
from app.security import decode_access_token

client_ip: ContextVar[str] = ContextVar("client_ip", default="")

_bearer = HTTPBearer(auto_error=False)


@dataclass
class Principal:
    """The signed-in user plus what they may do and which companies they may see."""

    user: User
    permissions: frozenset[str]
    company_ids: frozenset[int] | None = field(default=None)  # None = every company

    @property
    def id(self) -> int:
        return self.user.id

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def sees(self, company_id: int | None) -> bool:
        return self.company_ids is None or company_id in self.company_ids


def principal_for(user: User) -> Principal:
    perms = frozenset(p.key for p in user.role.permissions)
    companies = frozenset(c.id for c in user.companies) if user.companies else None
    return Principal(user, perms, companies)


def current_principal(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Principal:
    unauthorized = AppError(401, "unauthorized", "Please sign in.")
    if creds is None or creds.scheme.lower() != "bearer":
        raise unauthorized
    user_id = decode_access_token(creds.credentials)
    if user_id is None:
        raise AppError(401, "unauthorized", "Your session has expired. Please sign in again.")
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized
    p = principal_for(user)
    request.state.principal = p
    return p


def require(permission: str) -> Callable[..., Principal]:
    """Dependency: the signed-in user must hold this permission."""

    def check(p: Principal = Depends(current_principal)) -> Principal:
        if not p.can(permission):
            raise forbidden()
        return p

    check.__name__ = f"require_{permission.replace('.', '_')}"
    return check


def scope(p: Principal, company_id: int | None) -> list[int] | None:
    """Companies a list may show. None = no filter (all companies).
    Asking for a company the user may not see is refused."""
    if company_id is not None:
        if not p.sees(company_id):
            raise forbidden("You do not have access to this company.")
        return [company_id]
    return None if p.company_ids is None else sorted(p.company_ids)


def ensure_company(p: Principal, company_id: int | None, what: str = "Record") -> None:
    """For one record: a company the user may not see looks like it does not exist."""
    if not p.sees(company_id):
        raise AppError(404, "not_found", f"{what} not found.")


def visible_companies(db: Session, p: Principal) -> list[Company]:
    q = select(Company).order_by(Company.id)
    if p.company_ids is not None:
        q = q.where(Company.id.in_(p.company_ids))
    return list(db.scalars(q))


@dataclass
class PageParams:
    page: int
    size: int

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


def paging(page: int = Query(1, ge=1, le=100000), size: int = Query(50, ge=1, le=500)) -> PageParams:
    return PageParams(page, size)
