"""Sign-in with lockout, refresh-token rotation with reuse detection, sign-out, password change."""

from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import schemas as S
from app.clock import now_utc
from app.config import get_settings
from app.deps import principal_for, visible_companies
from app.errors import AppError
from app.models import RefreshToken, User
from app.security import create_access_token, hash_password, hash_token, new_refresh_token, verify_password
from app.services import audit

# Verified against when the username does not exist, so a wrong username takes as long as a wrong password.
_DUMMY_HASH = hash_password("not-a-real-password")


def _bad_login() -> AppError:
    return AppError(401, "invalid_login", "Wrong username or password.")


def me(db: Session, user: User) -> S.MeOut:
    p = principal_for(user)
    comps = visible_companies(db, p)
    return S.MeOut(
        id=user.id,
        username=user.username,
        full_name=user.full_name,
        email=user.email,
        role=S.RoleRef(id=user.role.id, key=user.role.key, name=user.role.name),
        permissions=sorted(p.permissions),
        companies=[S.CompanyOut.model_validate(c) for c in comps],
        all_companies=p.company_ids is None,
    )


def login(db: Session, username: str, password: str, user_agent: str) -> tuple[S.TokenOut, str]:
    s = get_settings()
    now = now_utc()
    user = db.scalar(select(User).where(User.username == username))
    if user is None:
        verify_password(password, _DUMMY_HASH)
        raise _bad_login()
    if user.locked_until and user.locked_until > now:
        raise AppError(423, "locked", "Too many failed sign-ins. Try again in a few minutes or ask the administrator.")
    if not verify_password(password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= s.max_failed_logins:
            user.locked_until = now + timedelta(minutes=s.lockout_minutes)
            user.failed_logins = 0
            audit.record(db, user.id, "lock", "user", user.id)
        db.commit()
        raise _bad_login()
    if not user.is_active:
        raise AppError(403, "inactive", "This account is switched off. Ask the administrator.")
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    token = issue_refresh(db, user, user_agent)
    audit.record(db, user.id, "login", "user", user.id)
    db.commit()
    return token_out(db, user), token


def issue_refresh(db: Session, user: User, user_agent: str) -> str:
    token, digest = new_refresh_token()
    db.add(
        RefreshToken(
            user_id=user.id,
            token_hash=digest,
            expires_at=now_utc() + timedelta(days=get_settings().refresh_token_days),
            user_agent=user_agent[:300],
        )
    )
    return token


def token_out(db: Session, user: User) -> S.TokenOut:
    s = get_settings()
    return S.TokenOut(
        access_token=create_access_token(user.id), expires_in=s.access_token_minutes * 60, user=me(db, user)
    )


def revoke_all(db: Session, user_id: int) -> None:
    db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=now_utc())
    )


def refresh(db: Session, token: str | None, user_agent: str) -> tuple[S.TokenOut, str]:
    expired = AppError(401, "unauthorized", "Your session has expired. Please sign in again.")
    if not token:
        raise expired
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(token)))
    now = now_utc()
    if row is None:
        raise expired
    if row.revoked_at is not None:
        # A token that was already swapped is being used again: it may have been stolen. End every session of this user.
        revoke_all(db, row.user_id)
        audit.record(db, row.user_id, "token_reuse", "user", row.user_id)
        db.commit()
        raise expired
    if row.expires_at <= now:
        raise expired
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        raise expired
    row.revoked_at = now
    new = issue_refresh(db, user, user_agent)
    db.commit()
    return token_out(db, user), new


def logout(db: Session, token: str | None) -> None:
    if not token:
        return
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(token)))
    if row is not None and row.revoked_at is None:
        row.revoked_at = now_utc()
        audit.record(db, row.user_id, "logout", "user", row.user_id)
        db.commit()


def change_password(db: Session, user: User, current: str, new: str) -> None:
    if not verify_password(current, user.password_hash):
        raise AppError(422, "invalid", "The current password is wrong.", {"field": "currentPassword"})
    if current == new:
        raise AppError(422, "invalid", "The new password must be different.", {"field": "newPassword"})
    user.password_hash = hash_password(new)
    revoke_all(db, user.id)
    audit.record(db, user.id, "change_password", "user", user.id)
    db.commit()
