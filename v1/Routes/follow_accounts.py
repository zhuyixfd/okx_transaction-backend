from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from config.db import get_db, SessionLocal
from config.constant import config as db_config
from v1.Models.follow_account import FollowAccount
from v1.Schema.follow_account import (
    FollowAccountCreate,
    FollowAccountDeleteOut,
    FollowAccountOut,
)


router = APIRouter(prefix="/follow-accounts", tags=["follow-accounts"])


SEED_LINKS = [
    "https://oyidl.net/ul/AeAkVdJ",
    "https://oyidl.net/ul/4DtIo37",
    "https://oyidl.net/ul/jBHQomm",
    "https://oyidl.net/ul/Jwv4rW4",
    "https://oyidl.net/ul/RZb96xX",
    "https://oyidl.net/ul/8ohvYTn",
]


def seed_follow_accounts(db: Session) -> None:
    """
    Insert seed rows for follow accounts.
    - If a row with the same `link` already exists, it will be skipped.
    """
    for link in SEED_LINKS:
        exists = db.execute(select(FollowAccount).where(FollowAccount.link == str(link))).scalar_one_or_none()
        if exists is None:
            db.add(FollowAccount(link=str(link)))
    db.commit()


def ensure_mysql_db_configured() -> None:
    if not db_config.MYSQL_DB:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="MYSQL_DB 未配置：请在 backend/.env 中填写要使用的数据库名后重启",
        )


@router.post("", response_model=FollowAccountOut, status_code=status.HTTP_201_CREATED)
def create_follow_account(payload: FollowAccountCreate, db: Session = Depends(get_db)) -> FollowAccountOut:
    ensure_mysql_db_configured()
    existing = db.execute(select(FollowAccount).where(FollowAccount.link == str(payload.link))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="link already exists")

    row = FollowAccount(link=str(payload.link))
    db.add(row)
    db.commit()
    db.refresh(row)
    return FollowAccountOut(id=row.id, link=row.link, created_at=row.created_at)


@router.get("", response_model=List[FollowAccountOut])
def list_follow_accounts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> List[FollowAccountOut]:
    ensure_mysql_db_configured()
    rows = db.execute(
        select(FollowAccount).order_by(FollowAccount.id).limit(limit).offset(offset)
    ).scalars().all()
    return [FollowAccountOut(id=r.id, link=r.link, created_at=r.created_at) for r in rows]


@router.get("/{account_id}", response_model=FollowAccountOut)
def get_follow_account(account_id: int, db: Session = Depends(get_db)) -> FollowAccountOut:
    ensure_mysql_db_configured()
    row = db.execute(select(FollowAccount).where(FollowAccount.id == account_id)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return FollowAccountOut(id=row.id, link=row.link, created_at=row.created_at)


@router.delete("/{account_id}", response_model=FollowAccountDeleteOut)
def delete_follow_account(account_id: int, db: Session = Depends(get_db)) -> FollowAccountDeleteOut:
    ensure_mysql_db_configured()
    row = db.execute(select(FollowAccount).where(FollowAccount.id == account_id)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    db.delete(row)
    db.commit()
    return FollowAccountDeleteOut(ok=True, id=account_id)


@router.get("/_seed", include_in_schema=False)
def seed_endpoint() -> dict:
    """
    Internal endpoint for debugging seeding. Not part of the public API.
    """
    db = SessionLocal()
    try:
        seed_follow_accounts(db)
        return {"ok": True, "seeded": True}
    finally:
        db.close()

