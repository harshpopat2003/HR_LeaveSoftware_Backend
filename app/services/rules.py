"""Rule sets: one group-wide set plus an optional override per company. Each save is a new version."""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import RuleSet
from app.rules.leave_rules import DEFAULT_RULES, RulesConfig


def latest(db: Session, company_id: int | None) -> RuleSet | None:
    cond = RuleSet.company_id.is_(None) if company_id is None else RuleSet.company_id == company_id
    return db.scalars(select(RuleSet).where(cond).order_by(RuleSet.version.desc()).limit(1)).first()


def effective(db: Session, company_id: int | None) -> tuple[RulesConfig, RuleSet | None, bool]:
    """(rules, row they came from, inherited from the group set?)."""
    if company_id is not None:
        own = latest(db, company_id)
        if own is not None:
            return RulesConfig.model_validate(own.config), own, False
    group = latest(db, None)
    if group is None:
        return DEFAULT_RULES, None, company_id is not None
    return RulesConfig.model_validate(group.config), group, company_id is not None


def for_company(db: Session, company_id: int) -> RulesConfig:
    return effective(db, company_id)[0]


class RulesCache:
    """Per-request cache so list screens look rules up once per company."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self._cache: dict[int, RulesConfig] = {}

    def get(self, company_id: int) -> RulesConfig:
        if company_id not in self._cache:
            self._cache[company_id] = for_company(self.db, company_id)
        return self._cache[company_id]


def save(db: Session, company_id: int | None, config: RulesConfig, user_id: int) -> RuleSet:
    cond = RuleSet.company_id.is_(None) if company_id is None else RuleSet.company_id == company_id
    current = db.scalar(select(func.max(RuleSet.version)).where(cond)) or 0
    row = RuleSet(company_id=company_id, config=config.model_dump(mode="json"), version=current + 1, created_by=user_id)
    db.add(row)
    db.flush()
    return row


def history(db: Session, company_id: int | None) -> list[RuleSet]:
    cond = RuleSet.company_id.is_(None) if company_id is None else RuleSet.company_id == company_id
    return list(db.scalars(select(RuleSet).where(cond).order_by(RuleSet.version.desc())))
