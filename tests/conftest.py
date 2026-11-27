"""Test setup: a real PostgreSQL database (TEST_DATABASE_URL, default hr_test).

Once per run: the schema is built with the Alembic migrations (up, down and up again) and the demo data is loaded.
Each test then runs inside a transaction that is rolled back, so tests never see each other's changes.
"""

import os
from collections.abc import Iterator
from datetime import date

from app.config import get_settings

# Point the app at the test database before anything creates the engine.
_test_url = os.environ.get("TEST_DATABASE_URL") or get_settings().test_database_url
os.environ["DATABASE_URL"] = _test_url
get_settings.cache_clear()
assert "test" in _test_url.rsplit("/", 1)[-1], "Refusing to run tests on a database whose name does not contain 'test'."

import pytest  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from alembic import command  # noqa: E402
from app import clock, seed  # noqa: E402
from app.db import engine, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Employee, User  # noqa: E402
from app.security import create_access_token  # noqa: E402

TODAY = date(2026, 11, 27)  # the demo data is written around this date


def alembic_config() -> Config:
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    cfg.attributes["db_url"] = _test_url
    cfg.attributes["configure_logger"] = False
    return cfg


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    with engine.begin() as c:
        c.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public"))
    cfg = alembic_config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")  # the downgrade must work too
    command.upgrade(cfg, "head")
    with Session(engine) as db:
        seed.run(db, demo=True)
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def frozen_today() -> Iterator[date]:
    clock.freeze(TODAY)
    yield TODAY
    clock.freeze(None)


@pytest.fixture
def db() -> Iterator[Session]:
    conn = engine.connect()
    outer = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint", autoflush=False, expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        conn.close()


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _headers(db: Session, username: str) -> dict[str, str]:
    uid = db.scalar(select(User.id).where(User.username == username))
    assert uid is not None, username
    return {"Authorization": f"Bearer {create_access_token(uid)}"}


@pytest.fixture
def as_admin(db: Session) -> dict[str, str]:
    return _headers(db, "admin")


@pytest.fixture
def as_hr(db: Session) -> dict[str, str]:
    return _headers(db, "hr1")


@pytest.fixture
def as_manager(db: Session) -> dict[str, str]:
    return _headers(db, "manager")


@pytest.fixture
def headers_for(db: Session):
    return lambda username: _headers(db, username)


@pytest.fixture
def emp(db: Session):
    """Look up a demo employee by number."""

    def get(emp_no: str) -> Employee:
        e = db.scalar(select(Employee).where(Employee.emp_no == emp_no))
        assert e is not None, emp_no
        return e

    return get


def company_id(db: Session, code: str) -> int:
    from app.models import Company

    cid = db.scalar(select(Company.id).where(Company.code == code))
    assert cid is not None
    return cid
