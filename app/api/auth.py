from fastapi import APIRouter, Cookie, Depends, Request, Response
from sqlalchemy.orm import Session

from app import schemas as S
from app.config import get_settings
from app.db import get_db
from app.deps import Principal, current_principal
from app.services import auth

router = APIRouter(prefix="/auth", tags=["auth"])

COOKIE = "hr_refresh"
COOKIE_PATH = "/api/v1/auth"


def _set_cookie(resp: Response, token: str) -> None:
    s = get_settings()
    resp.set_cookie(
        COOKIE,
        token,
        max_age=s.refresh_token_days * 86400,
        httponly=True,
        secure=s.cookie_secure,
        samesite="strict",
        path=COOKIE_PATH,
    )


@router.post("/login", response_model=S.TokenOut)
def login(body: S.LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> S.TokenOut:
    out, token = auth.login(db, body.username, body.password, request.headers.get("user-agent", ""))
    _set_cookie(response, token)
    return out


@router.post("/refresh", response_model=S.TokenOut)
def refresh(
    request: Request, response: Response, db: Session = Depends(get_db), hr_refresh: str | None = Cookie(None)
) -> S.TokenOut:
    out, token = auth.refresh(db, hr_refresh, request.headers.get("user-agent", ""))
    _set_cookie(response, token)
    return out


@router.post("/logout", status_code=204)
def logout(response: Response, db: Session = Depends(get_db), hr_refresh: str | None = Cookie(None)) -> Response:
    auth.logout(db, hr_refresh)
    response.status_code = 204
    response.delete_cookie(COOKIE, path=COOKIE_PATH)
    return response


@router.post("/change-password", status_code=204)
def change_password(
    body: S.ChangePasswordIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)
) -> Response:
    auth.change_password(db, p.user, body.current_password, body.new_password)
    resp = Response(status_code=204)
    resp.delete_cookie(COOKIE, path=COOKIE_PATH)
    return resp


@router.get("/me", response_model=S.MeOut)
def me(p: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> S.MeOut:
    return auth.me(db, p.user)
