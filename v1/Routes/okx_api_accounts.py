from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.db import get_db
from config.constant import config as db_config
from v1.Models.follow_account import FollowAccount
from v1.Models.okx_api_account import OkxApiAccount
from v1.Schema.okx_api_account import (
    OKX_SECRET_MASK,
    OkxApiAccountCreate,
    OkxApiAccountDeleteOut,
    OkxApiAccountOut,
    OkxApiAccountPatch,
)

router = APIRouter(prefix="/okx-api-accounts", tags=["okx-api-accounts"])


def _ensure_db() -> None:
    if not db_config.MYSQL_DB:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="MYSQL_DB 未配置：请在 backend/.env 中填写要使用的数据库名后重启",
        )


def _bound_info(db: Session, okx_id: int) -> tuple[int | None, str | None]:
    row = (
        db.execute(
            select(FollowAccount).where(FollowAccount.okx_api_account_id == okx_id).limit(1)
        )
        .scalars()
        .first()
    )
    if row is None:
        return None, None
    label = (row.nickname or "").strip() or (row.unique_name or "").strip() or None
    return row.id, label


def _to_out(db: Session, r: OkxApiAccount) -> OkxApiAccountOut:
    bid, blabel = _bound_info(db, r.id)
    return OkxApiAccountOut(
        id=r.id,
        okx_follow_api_key=OKX_SECRET_MASK,
        okx_follow_secret_key=OKX_SECRET_MASK,
        okx_follow_passphrase=OKX_SECRET_MASK,
        okx_follow_api_label=r.api_label,
        remark=r.remark,
        created_at=r.created_at,
        bound_follow_account_id=bid,
        bound_trader_label=blabel,
    )


@router.get("", response_model=List[OkxApiAccountOut])
def list_okx_api_accounts(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> List[OkxApiAccountOut]:
    _ensure_db()
    rows = (
        db.execute(select(OkxApiAccount).order_by(OkxApiAccount.id.desc()).limit(limit).offset(offset))
        .scalars()
        .all()
    )
    return [_to_out(db, r) for r in rows]


@router.post("", response_model=OkxApiAccountOut, status_code=status.HTTP_201_CREATED)
def create_okx_api_account(
    payload: OkxApiAccountCreate,
    db: Session = Depends(get_db),
) -> OkxApiAccountOut:
    _ensure_db()
    row = OkxApiAccount(
        api_key=payload.okx_follow_api_key.strip(),
        api_secret=payload.okx_follow_secret_key.strip(),
        api_passphrase=payload.okx_follow_passphrase.strip(),
        api_label=(payload.okx_follow_api_label or "").strip() or None,
        remark=(payload.remark or "").strip() or None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(db, row)


@router.get("/{account_id}", response_model=OkxApiAccountOut)
def get_okx_api_account(account_id: int, db: Session = Depends(get_db)) -> OkxApiAccountOut:
    _ensure_db()
    row = db.get(OkxApiAccount, account_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return _to_out(db, row)


@router.patch("/{account_id}", response_model=OkxApiAccountOut)
def patch_okx_api_account(
    account_id: int,
    payload: OkxApiAccountPatch,
    db: Session = Depends(get_db),
) -> OkxApiAccountOut:
    _ensure_db()
    row = db.get(OkxApiAccount, account_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    data = payload.model_dump(exclude_unset=True)
    if "okx_follow_api_key" in data and data["okx_follow_api_key"] is not None:
        s = str(data["okx_follow_api_key"]).strip()
        if s:
            row.api_key = s
    if "okx_follow_secret_key" in data and data["okx_follow_secret_key"] is not None:
        s = str(data["okx_follow_secret_key"]).strip()
        if s:
            row.api_secret = s
    if "okx_follow_passphrase" in data and data["okx_follow_passphrase"] is not None:
        s = str(data["okx_follow_passphrase"]).strip()
        if s:
            row.api_passphrase = s
    if "okx_follow_api_label" in data:
        v = data["okx_follow_api_label"]
        row.api_label = (str(v).strip() if v is not None else "") or None
    if "remark" in data:
        v = data["remark"]
        row.remark = (str(v).strip() if v is not None else "") or None

    db.commit()
    db.refresh(row)
    return _to_out(db, row)


@router.delete("/{account_id}", response_model=OkxApiAccountDeleteOut)
def delete_okx_api_account(account_id: int, db: Session = Depends(get_db)) -> OkxApiAccountDeleteOut:
    _ensure_db()
    row = db.get(OkxApiAccount, account_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    bound = (
        db.execute(
            select(FollowAccount.id).where(FollowAccount.okx_api_account_id == account_id).limit(1)
        )
        .scalar_one_or_none()
    )
    if bound is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该 API 帐户仍绑定在某个交易员跟单记录上，请先解除绑定再删除",
        )
    db.delete(row)
    db.commit()
    return OkxApiAccountDeleteOut(ok=True, id=account_id)
