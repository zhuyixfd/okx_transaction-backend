from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from config.cn_time import now_cn
from config.db import get_db
from config.constant import config as db_config
from module import OkxTrade
from v1.Models.follow_account import FollowAccount
from v1.Models.follow_position import FollowPositionEvent, FollowPositionSnapshot
from v1.Models.follow_sim_record import FollowSimRecord
from v1.Schema.follow_account import (
    FollowAccountCreate,
    FollowAccountDeleteOut,
    FollowAccountOut,
    FollowAccountPatch,
    FollowConfigPatch,
)
from v1.Schema.position_event import PositionEventOut, PositionEventPageOut
from v1.Schema.follow_sim_record import FollowSimRecordOut, FollowSimRecordsPageOut
from v1.Schema.position_pnl_summary import PnlTotalsBlock, PositionPnlSummaryOut
from v1.Schema.position_snapshot import PositionSnapshotItem, PositionSnapshotOut
from v1.Services.position_monitor import _sim_pnl_usdt


router = APIRouter(prefix="/follow-accounts", tags=["follow-accounts"])


def _upl_ratio_from_detail_json(detail_json: str | None) -> str | None:
    if not detail_json or not str(detail_json).strip():
        return None
    try:
        d = json.loads(detail_json)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    v = d.get("uplRatio")
    if v is None:
        v = d.get("upl_ratio")
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _upl_from_detail_json(detail_json: str | None) -> str | None:
    if not detail_json or not str(detail_json).strip():
        return None
    try:
        d = json.loads(detail_json)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    v = d.get("upl")
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _detail_str_field(detail_json: str | None, key: str) -> str | None:
    if not detail_json or not str(detail_json).strip():
        return None
    try:
        d = json.loads(detail_json)
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    v = d.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _row_str(row: dict, key: str) -> str | None:
    v = row.get(key)
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _normalize_link(url: str) -> str:
    return str(url).strip().rstrip("/")


def _to_out(
    row: FollowAccount,
    *,
    positions_refreshed_at: datetime | None = None,
) -> FollowAccountOut:
    return FollowAccountOut(
        id=row.id,
        link=row.link,  # type: ignore[arg-type]
        nickname=row.nickname,
        unique_name=row.unique_name,
        enabled=row.enabled,
        last_enabled_at=row.last_enabled_at,
        created_at=row.created_at,
        positions_refreshed_at=positions_refreshed_at,
        bet_amount_per_position=row.bet_amount_per_position,
        max_follow_positions=row.max_follow_positions,
        bet_mode=row.bet_mode or "cost",
        margin_add_ratio_of_bet=row.margin_add_ratio_of_bet
        if row.margin_add_ratio_of_bet is not None
        else Decimal("0.2"),
        margin_auto_enabled=bool(row.margin_auto_enabled),
        margin_add_max_times=row.margin_add_max_times,
    )


def _snapshot_refreshed_at(db: Session, account_id: int) -> datetime | None:
    snap = db.get(FollowPositionSnapshot, account_id)
    return snap.updated_at if snap else None


def ensure_mysql_db_configured() -> None:
    if not db_config.MYSQL_DB:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="MYSQL_DB 未配置：请在 backend/.env 中填写要使用的数据库名后重启",
        )


@router.post("", response_model=FollowAccountOut, status_code=status.HTTP_201_CREATED)
async def create_follow_account(
    payload: FollowAccountCreate,
    db: Session = Depends(get_db),
) -> FollowAccountOut:
    ensure_mysql_db_configured()
    url = _normalize_link(str(payload.link))
    existing = (
        db.execute(select(FollowAccount).where(FollowAccount.link == url))
        .scalar_one_or_none()
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该链接已存在")

    nickname, unique_name = await OkxTrade.get_uniqueName(url)
    if not unique_name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="无法从页面解析该链接对应的 uniqueName，请确认链接可访问且格式正确",
        )

    row = FollowAccount(
        link=url,
        nickname=nickname,
        unique_name=unique_name,
        enabled=False,
        last_enabled_at=None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _to_out(row)


@router.get("", response_model=List[FollowAccountOut])
def list_follow_accounts(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    enabled_only: bool | None = Query(
        None,
        description="为 true 时仅返回已启用的帐号",
    ),
    nickname_contains: str | None = Query(
        None,
        max_length=128,
        description="昵称模糊匹配（包含即命中，忽略首尾空格）",
    ),
    db: Session = Depends(get_db),
) -> List[FollowAccountOut]:
    ensure_mysql_db_configured()
    stmt = select(FollowAccount, FollowPositionSnapshot.updated_at).outerjoin(
        FollowPositionSnapshot,
        FollowAccount.id == FollowPositionSnapshot.follow_account_id,
    )

    if enabled_only is True:
        stmt = stmt.where(FollowAccount.enabled == True)  # noqa: E712

    if nickname_contains is not None and nickname_contains.strip():
        kw = f"%{nickname_contains.strip()}%"
        stmt = stmt.where(FollowAccount.nickname.isnot(None)).where(FollowAccount.nickname.like(kw))

    # 启用优先（True 在前），同组内按最近一次启用时间倒序；时间为 NULL 的排在同组末尾
    stmt = (
        stmt.order_by(
            desc(FollowAccount.enabled),
            desc(FollowAccount.last_enabled_at),
        )
        .limit(limit)
        .offset(offset)
    )

    pairs = db.execute(stmt).all()
    return [
        _to_out(acc, positions_refreshed_at=ref_at)
        for acc, ref_at in pairs
    ]


def _snapshot_row_to_item(row: dict) -> PositionSnapshotItem:
    ur = row.get("uplRatio")
    if ur is None:
        ur = row.get("upl_ratio")
    upl_s = None if ur is None else (str(ur).strip() or None)
    uu = row.get("upl")
    upl_usdt = None if uu is None else (str(uu).strip() or None)
    return PositionSnapshotItem(
        pos_id=str(row.get("posId", "")),
        c_time=row.get("cTime"),
        c_time_format=row.get("cTime_format"),
        pos_ccy=row.get("posCcy"),
        pos_side=row.get("posSide"),
        lever=row.get("lever"),
        avg_px=row.get("avgPx"),
        last_px=row.get("last"),
        upl_ratio=upl_s,
        upl=upl_usdt,
        pos=_row_str(row, "pos"),
        margin=_row_str(row, "margin"),
        mgn_ratio=_row_str(row, "mgnRatio"),
        liq_px=_row_str(row, "liqPx"),
    )


def _event_to_out(r: FollowPositionEvent) -> PositionEventOut:
    dj = r.detail_json
    return PositionEventOut(
        id=r.id,
        follow_account_id=r.follow_account_id,
        unique_name=r.unique_name,
        event_type=r.event_type,
        pos_id=r.pos_id,
        pos_ccy=r.pos_ccy,
        pos_side=r.pos_side,
        lever=r.lever,
        avg_px=r.avg_px,
        last_px=r.last_px,
        upl_ratio=_upl_ratio_from_detail_json(dj),
        upl=_upl_from_detail_json(dj),
        pos=_detail_str_field(dj, "pos"),
        margin=_detail_str_field(dj, "margin"),
        mgn_ratio=_detail_str_field(dj, "mgnRatio"),
        liq_px=_detail_str_field(dj, "liqPx"),
        c_time=r.c_time,
        detail_json=dj,
        created_at=r.created_at,
    )


@router.get("/position-events", response_model=PositionEventPageOut)
def list_position_events(
    unique_name: str = Query(..., min_length=1, max_length=128, description="跟单帐户 uniqueName"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> PositionEventPageOut:
    """查询某帐户的持仓监控记录（新在前），返回分页与总数。"""
    ensure_mysql_db_configured()
    un = unique_name.strip()
    base = FollowPositionEvent.unique_name == un
    total = int(
        db.execute(select(func.count()).select_from(FollowPositionEvent).where(base)).scalar_one()
    )
    rows = (
        db.execute(
            select(FollowPositionEvent)
            .where(base)
            .order_by(desc(FollowPositionEvent.id))
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return PositionEventPageOut(items=[_event_to_out(r) for r in rows], total=total)


@router.get("/position-snapshot", response_model=PositionSnapshotOut)
def get_position_snapshot(
    unique_name: str = Query(..., min_length=1, max_length=128, description="跟单帐户 uniqueName"),
    db: Session = Depends(get_db),
) -> PositionSnapshotOut:
    """
    返回该帐户最近一次监控轮询写入的持仓快照（含标记价 last_px）及刷新时间。
    与 `follow_position_snapshots` 表一致；启用帐户由后台持续更新。
    """
    ensure_mysql_db_configured()
    un = unique_name.strip()
    acc = (
        db.execute(select(FollowAccount).where(FollowAccount.unique_name == un))
        .scalar_one_or_none()
    )
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    snap = db.get(FollowPositionSnapshot, acc.id)
    if snap is None:
        return PositionSnapshotOut(unique_name=un, refreshed_at=None, positions=[])

    try:
        raw: dict = json.loads(snap.snapshot_json)
    except Exception:
        raw = {}

    items: list[PositionSnapshotItem] = []
    for _pid in sorted(raw.keys(), key=lambda k: (len(str(k)), str(k))):
        row = raw.get(_pid)
        if isinstance(row, dict) and row.get("posId") is not None:
            items.append(_snapshot_row_to_item(row))

    return PositionSnapshotOut(
        unique_name=un,
        refreshed_at=snap.updated_at,
        positions=items,
    )


@router.get("/position-pnl-summary", response_model=PositionPnlSummaryOut)
def get_position_pnl_summary(
    unique_name: str = Query(..., min_length=1, max_length=128, description="跟单帐户 uniqueName"),
    db: Session = Depends(get_db),
) -> PositionPnlSummaryOut:
    """
    按「每个仓位下注金额」用与模拟跟单相同的公式汇总：
    - holdings：当前快照各仓位的浮动盈亏合计（已实现恒为 0）。
    - ledger：全部平仓记录的已实现盈亏合计 + 当前快照浮动（总收益=二者之和）。
    """
    ensure_mysql_db_configured()
    un = unique_name.strip()
    acc = (
        db.execute(select(FollowAccount).where(FollowAccount.unique_name == un))
        .scalar_one_or_none()
    )
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    stake = (
        Decimal(str(acc.bet_amount_per_position))
        if acc.bet_amount_per_position is not None
        else Decimal(0)
    )

    unrealized_snap = Decimal(0)
    snap = db.get(FollowPositionSnapshot, acc.id)
    if snap is not None:
        try:
            raw: dict = json.loads(snap.snapshot_json)
        except Exception:
            raw = {}
        for _pid, row in raw.items():
            if not isinstance(row, dict) or row.get("posId") is None:
                continue
            unrealized_snap += _sim_pnl_usdt(
                stake,
                row.get("avgPx"),
                row.get("last"),
                row.get("posSide"),
            )

    realized_close = Decimal(0)
    close_rows = (
        db.execute(
            select(FollowPositionEvent).where(
                FollowPositionEvent.unique_name == un,
                FollowPositionEvent.event_type == "close",
            )
        )
        .scalars()
        .all()
    )
    for ev in close_rows:
        realized_close += _sim_pnl_usdt(stake, ev.avg_px, ev.last_px, ev.pos_side)

    total_ledger = realized_close + unrealized_snap

    holdings = PnlTotalsBlock(
        total_pnl_usdt=format(unrealized_snap, "f"),
        realized_sum_usdt=format(Decimal(0), "f"),
        unrealized_sum_usdt=format(unrealized_snap, "f"),
    )
    ledger = PnlTotalsBlock(
        total_pnl_usdt=format(total_ledger, "f"),
        realized_sum_usdt=format(realized_close, "f"),
        unrealized_sum_usdt=format(unrealized_snap, "f"),
    )
    return PositionPnlSummaryOut(holdings=holdings, ledger=ledger)


def _sim_to_out(r: FollowSimRecord) -> FollowSimRecordOut:
    return FollowSimRecordOut(
        id=r.id,
        follow_account_id=r.follow_account_id,
        pos_id=r.pos_id,
        pos_ccy=r.pos_ccy,
        pos_side=r.pos_side,
        entry_avg_px=r.entry_avg_px,
        stake_usdt=r.stake_usdt,
        status=r.status,
        exit_px=r.exit_px,
        realized_pnl_usdt=r.realized_pnl_usdt,
        unrealized_pnl_usdt=r.unrealized_pnl_usdt,
        last_mark_px=r.last_mark_px,
        src_pos=r.src_pos,
        src_margin=r.src_margin,
        src_mgn_ratio=r.src_mgn_ratio,
        src_liq_px=r.src_liq_px,
        opened_at=r.opened_at,
        closed_at=r.closed_at,
        updated_at=r.updated_at,
    )


@router.get("/follow-sim-records", response_model=FollowSimRecordsPageOut)
def list_follow_sim_records(
    unique_name: str = Query(..., min_length=1, max_length=128, description="跟单帐户 uniqueName"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> FollowSimRecordsPageOut:
    """模拟跟单资金记录：在跟仓位为浮动盈亏，已平仓为已实现盈亏；返回帐户级总收益。"""
    ensure_mysql_db_configured()
    un = unique_name.strip()
    acc = (
        db.execute(select(FollowAccount).where(FollowAccount.unique_name == un))
        .scalar_one_or_none()
    )
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    base = FollowSimRecord.follow_account_id == acc.id
    total = int(
        db.execute(select(func.count()).select_from(FollowSimRecord).where(base)).scalar_one()
    )
    rs = db.execute(
        select(func.coalesce(func.sum(FollowSimRecord.realized_pnl_usdt), 0)).where(
            base,
            FollowSimRecord.status == "closed",
        )
    ).scalar_one()
    us = db.execute(
        select(func.coalesce(func.sum(FollowSimRecord.unrealized_pnl_usdt), 0)).where(
            base,
            FollowSimRecord.status == "open",
        )
    ).scalar_one()
    rd = Decimal(str(rs)) if rs is not None else Decimal(0)
    ud = Decimal(str(us)) if us is not None else Decimal(0)
    total_pnl = rd + ud

    rows = (
        db.execute(
            select(FollowSimRecord)
            .where(base)
            .order_by(desc(FollowSimRecord.id))
            .limit(limit)
            .offset(offset)
        )
        .scalars()
        .all()
    )
    return FollowSimRecordsPageOut(
        items=[_sim_to_out(r) for r in rows],
        total=total,
        total_pnl_usdt=format(total_pnl, "f"),
        realized_sum_usdt=format(rd, "f"),
        unrealized_sum_usdt=format(ud, "f"),
    )


@router.patch("/{account_id}/follow-config", response_model=FollowAccountOut)
def patch_follow_config(
    account_id: int,
    payload: FollowConfigPatch,
    db: Session = Depends(get_db),
) -> FollowAccountOut:
    """更新跟单配置（仓位数量、下注金额、保证金率监控与追加比例等）。"""
    ensure_mysql_db_configured()
    row = (
        db.execute(select(FollowAccount).where(FollowAccount.id == account_id))
        .scalar_one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    data = payload.model_dump(exclude_unset=True)
    if "bet_mode" in data and data["bet_mode"] is not None and data["bet_mode"] != "cost":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="当前仅支持 bet_mode=cost（按成本下单）",
        )
    for key, val in data.items():
        setattr(row, key, val)
    db.commit()
    db.refresh(row)
    return _to_out(row, positions_refreshed_at=_snapshot_refreshed_at(db, row.id))


@router.get("/{account_id}", response_model=FollowAccountOut)
def get_follow_account(account_id: int, db: Session = Depends(get_db)) -> FollowAccountOut:
    ensure_mysql_db_configured()
    row = (
        db.execute(select(FollowAccount).where(FollowAccount.id == account_id))
        .scalar_one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return _to_out(row, positions_refreshed_at=_snapshot_refreshed_at(db, row.id))


@router.patch("/{account_id}", response_model=FollowAccountOut)
def patch_follow_account(
    account_id: int,
    payload: FollowAccountPatch,
    db: Session = Depends(get_db),
) -> FollowAccountOut:
    ensure_mysql_db_configured()
    row = (
        db.execute(select(FollowAccount).where(FollowAccount.id == account_id))
        .scalar_one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    if payload.enabled and not row.enabled:
        row.last_enabled_at = now_cn()
    row.enabled = payload.enabled
    db.commit()
    db.refresh(row)
    return _to_out(row, positions_refreshed_at=_snapshot_refreshed_at(db, row.id))


@router.delete("/{account_id}", response_model=FollowAccountDeleteOut)
def delete_follow_account(account_id: int, db: Session = Depends(get_db)) -> FollowAccountDeleteOut:
    ensure_mysql_db_configured()
    row = (
        db.execute(select(FollowAccount).where(FollowAccount.id == account_id))
        .scalar_one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if row.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="已启用的帐户不能删除，请先关闭启用",
        )

    db.delete(row)
    db.commit()
    return FollowAccountDeleteOut(ok=True, id=account_id)
