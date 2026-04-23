from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from config.cn_time import now_cn
from config.db import get_db
from config.constant import config as db_config
from module import OkxTrade
from module.follow_order import OkxFollowOrderClient
from v1.Models.follow_account import FollowAccount
from v1.Models.okx_api_account import OkxApiAccount
from v1.Models.follow_position import FollowPositionEvent, FollowPositionSnapshot
from v1.Models.follow_sim_record import FollowSimRecord
from v1.Schema.follow_account import (
    FollowAccountCreate,
    FollowAccountDeleteOut,
    FollowAccountOut,
    FollowAccountPatch,
    FollowConfigPatch,
)
from v1.Schema.okx_api_account import FollowAccountOkxBindPatch
from v1.Schema.position_event import PositionEventOut, PositionEventPageOut
from v1.Schema.follow_sim_record import (
    FollowSimRecordDeleteOut,
    FollowSimRecordOut,
    FollowSimRecordsPageOut,
)
from v1.Schema.position_pnl_summary import PnlTotalsBlock, PositionPnlSummaryOut
from v1.Schema.position_snapshot import PositionSnapshotItem, PositionSnapshotOut
from v1.Services.position_monitor import _sim_pnl_usdt
from v1.Services.okx_account_client import require_okx_client
from v1.Services.okx_contract_helpers import (
    normalize_swap_inst_id,
    parse_account_config_fields,
    sizing_lever_from_leverage_info,
)


router = APIRouter(prefix="/follow-accounts", tags=["follow-accounts"])


class PositionActionBody(BaseModel):
    unique_name: str = Field(..., min_length=1, max_length=128)
    sim_record_id: int | None = Field(None, ge=1)
    pos_ccy: str | None = Field(None, min_length=1, max_length=32)
    pos_side: str | None = Field(None, pattern="^(long|short)$")
    action: str = Field(..., pattern="^(add|reduce|close|reverse)$")


class SnapshotFollowBody(BaseModel):
    unique_name: str = Field(..., min_length=1, max_length=128)
    pos_id: str = Field(..., min_length=1, max_length=64)


class SnapshotFollowSideBody(BaseModel):
    unique_name: str = Field(..., min_length=1, max_length=128)
    pos_ccy: str = Field(..., min_length=1, max_length=32)
    pos_side: str = Field(..., pattern="^(long|short)$")


def _side_block_pid(ccy: str, side: str) -> str:
    return f"__side_block__:{ccy.strip().upper()}:{side.strip().lower()}"


def _snapshot_ct_key(row: dict) -> tuple[int, str]:
    ct = row.get("cTime")
    try:
        ct_i = int(ct) if ct is not None and str(ct).strip() != "" else 0
    except (TypeError, ValueError):
        ct_i = 0
    return (ct_i, str(row.get("posId", "")))


def _to_decimal_safe(raw: object) -> Decimal | None:
    try:
        s = str(raw).strip()
        if not s:
            return None
        return Decimal(s)
    except Exception:
        return None


def _infer_net_side_display(
    *,
    raw_side: str,
    avg_px_raw: object,
    mark_px_raw: object,
    upl_raw: object,
) -> str:
    side = (raw_side or "").strip().lower()
    if side in ("long", "short"):
        return side
    if side != "net":
        return side
    avg_px = _to_decimal_safe(avg_px_raw)
    mark_px = _to_decimal_safe(mark_px_raw)
    upl = _to_decimal_safe(upl_raw)
    if avg_px is None or mark_px is None or upl is None:
        return side
    diff = mark_px - avg_px
    if diff == 0 or upl == 0:
        return side
    # 规则：同号=做多，异号=做空。
    return "long" if ((diff > 0) == (upl > 0)) else "short"


@router.get("/follow-diagnose")
async def follow_diagnose(
    unique_name: str = Query(..., min_length=1, max_length=128, description="跟单帐户 uniqueName"),
    db: Session = Depends(get_db),
) -> dict:
    """返回对方当前仓位逐条诊断：是否会跟、阻断原因。"""
    ensure_mysql_db_configured()
    un = unique_name.strip()
    acc = (
        db.execute(select(FollowAccount).where(FollowAccount.unique_name == un))
        .scalar_one_or_none()
    )
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    reasons_global: list[str] = []
    if not acc.enabled:
        reasons_global.append("account_disabled")
    if not bool(acc.live_trading_enabled):
        reasons_global.append("live_trading_disabled")
    if acc.okx_api_account_id is None:
        reasons_global.append("okx_api_unbound")
    coeff = Decimal(str(acc.open_by_asset_ratio_coeff or 0))
    if coeff <= 0:
        reasons_global.append("position_size_coeff_invalid")

    snap = db.get(FollowPositionSnapshot, acc.id)
    if snap is None:
        return {
            "unique_name": un,
            "global_reasons": reasons_global + ["no_snapshot"],
            "items": [],
        }
    try:
        snap_map = json.loads(snap.snapshot_json)
    except Exception:
        snap_map = {}
    if not isinstance(snap_map, dict):
        snap_map = {}

    rows = [r for r in snap_map.values() if isinstance(r, dict) and r.get("posId") is not None]
    rows.sort(key=_snapshot_ct_key)

    side_block_latest: dict[str, str] = {}
    side_rows = (
        db.execute(
            select(FollowSimRecord)
            .where(
                FollowSimRecord.follow_account_id == acc.id,
                FollowSimRecord.pos_id.like("__side_block__:%"),
            )
            .order_by(FollowSimRecord.id.desc())
        )
        .scalars()
        .all()
    )
    for r in side_rows:
        pid = str(r.pos_id or "").strip()
        if not pid.startswith("__side_block__:"):
            continue
        if pid not in side_block_latest:
            side_block_latest[pid] = str(r.status or "").strip().lower()

    max_n = acc.max_follow_positions
    selected_pids: set[str] = set()
    if max_n is None or int(max_n) <= 0:
        selected_pids = {
            str(r.get("posId", "")).strip()
            for r in rows
            if str(r.get("posId", "")).strip()
        }
    else:
        for r in rows:
            pid = str(r.get("posId", "")).strip()
            if pid:
                selected_pids.add(pid)
            if len(selected_pids) >= int(max_n):
                break

    own_inst_side: set[tuple[str, str]] = set()
    if acc.okx_api_account_id is not None:
        try:
            client = require_okx_client(db, acc.okx_api_account_id)
            ok_cfg, cfg_data = await client.get_account_config()
            _, cfg_pos_mode = parse_account_config_fields(cfg_data) if ok_cfg else (None, None)
            hedge_mode = cfg_pos_mode != "net_mode"
            ok_pos, pos_payload = await client.get_positions_inst("SWAP")
            if ok_pos and isinstance(pos_payload, dict):
                for p in pos_payload.get("data") or []:
                    if not isinstance(p, dict):
                        continue
                    inst = str(p.get("instId", "")).strip().upper()
                    try:
                        pos_v = float(str(p.get("pos", "")).strip() or "0")
                    except ValueError:
                        pos_v = 0.0
                    if not inst or abs(pos_v) < 1e-12:
                        continue
                    if hedge_mode:
                        ps = str(p.get("posSide", "")).strip().lower()
                        if ps in ("long", "short"):
                            own_inst_side.add((inst, ps))
                    else:
                        own_inst_side.add((inst, "long" if pos_v > 0 else "short"))
        except Exception:
            pass

    items: list[dict] = []
    for r in rows:
        pid = str(r.get("posId", "")).strip()
        ccy = str(r.get("posCcy", "")).strip().upper()
        side_raw = str(r.get("posSide", "")).strip().lower()
        side = _infer_net_side_display(
            raw_side=side_raw,
            avg_px_raw=r.get("avgPx"),
            mark_px_raw=r.get("last"),
            upl_raw=r.get("upl"),
        )
        inst_id = normalize_swap_inst_id(ccy) if ccy else ""
        rs = list(reasons_global)

        if not pid:
            rs.append("invalid_pos_id")
        if not ccy:
            rs.append("invalid_pos_ccy")
        if side not in ("long", "short"):
            rs.append("invalid_pos_side")
        if pid and pid not in selected_pids:
            rs.append("out_of_max_follow_positions")
        if ccy and side in ("long", "short"):
            block_key = _side_block_pid(ccy, side)
            if side_block_latest.get(block_key) == "closed":
                rs.append("paused_by_side_config")
        if inst_id and side in ("long", "short") and (inst_id, side) in own_inst_side:
            rs.append("already_has_own_position_same_inst_side")
        items.append(
            {
                "pos_id": pid,
                "pos_ccy": ccy or None,
                "pos_side": side or None,
                "will_follow": len(rs) == 0,
                "reasons": rs,
            }
        )

    return {
        "unique_name": un,
        "global_reasons": reasons_global,
        "items": items,
    }


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


def _snapshot_notional_usd(row: dict) -> str | None:
    for k in ("notionalUsd", "notional_usd", "notionalUSDT", "notional_usdt"):
        s = _row_str(row, k)
        if s is not None:
            return s
    # 兜底：若接口未给 notionalUsd，则按 持仓量 * 标记价格 估算
    pos_s = _row_str(row, "pos")
    last_s = _row_str(row, "last")
    try:
        pos_v = Decimal(str(pos_s)) if pos_s is not None else None
        last_v = Decimal(str(last_s)) if last_s is not None else None
    except Exception:
        pos_v = None
        last_v = None
    if pos_v is None or last_v is None:
        return None
    return format(abs(pos_v * last_v), "f")


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
        single_add_margin_usdt=row.bet_amount_per_position,
        max_follow_positions=row.max_follow_positions,
        bet_mode=row.bet_mode or "cost",
        margin_add_ratio_of_bet=row.margin_add_ratio_of_bet
        if row.margin_add_ratio_of_bet is not None
        else Decimal("1"),
        margin_auto_enabled=True,
        margin_add_max_times=row.margin_add_max_times,
        okx_api_account_id=row.okx_api_account_id,
        live_trading_enabled=bool(row.live_trading_enabled),
        position_size_coeff=row.open_by_asset_ratio_coeff
        if row.open_by_asset_ratio_coeff is not None
        else Decimal("1"),
        maint_margin_ratio_threshold=row.maint_margin_ratio_threshold,
        close_margin_ratio_threshold=row.close_margin_ratio_threshold,
        take_profit_ratio=row.take_profit_ratio,
        stop_loss_ratio=row.stop_loss_ratio,
    )


def _snapshot_refreshed_at(db: Session, account_id: int) -> datetime | None:
    snap = db.get(FollowPositionSnapshot, account_id)
    return snap.updated_at if snap else None


def _require_linked_okx_client(db: Session, unique_name: str) -> OkxFollowOrderClient:
    """当前交易员跟单已绑定的 OKX API 帐户 → OkxFollowOrderClient（密钥来自 DB，请求走 follow_order）。"""
    ensure_mysql_db_configured()
    un = unique_name.strip()
    acc = (
        db.execute(select(FollowAccount).where(FollowAccount.unique_name == un))
        .scalar_one_or_none()
    )
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    oid = acc.okx_api_account_id
    if oid is None:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="未绑定 OKX API 帐户，无法查询本人交易数据",
        )
    return require_okx_client(db, oid)


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
    pos_side_raw = row.get("posSide")
    pos_side_out = _infer_net_side_display(
        raw_side="" if pos_side_raw is None else str(pos_side_raw),
        avg_px_raw=row.get("avgPx"),
        mark_px_raw=row.get("last"),
        upl_raw=row.get("upl"),
    )
    return PositionSnapshotItem(
        pos_id=str(row.get("posId", "")),
        c_time=row.get("cTime"),
        c_time_format=row.get("cTime_format"),
        pos_ccy=row.get("posCcy"),
        pos_side=pos_side_out or None,
        lever=row.get("lever"),
        avg_px=row.get("avgPx"),
        last_px=row.get("last"),
        upl_ratio=upl_s,
        upl=upl_usdt,
        pos=_row_str(row, "pos"),
        notional_usd=_snapshot_notional_usd(row),
        notional_ccy=_row_str(row, "notionalCcy") or _row_str(row, "notional_ccy"),
        notional=_row_str(row, "notional"),
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
        notional_usd=_detail_str_field(dj, "notionalUsd") or _detail_str_field(dj, "notional_usd"),
        notional_ccy=_detail_str_field(dj, "notionalCcy") or _detail_str_field(dj, "notional_ccy"),
        notional=_detail_str_field(dj, "notional"),
        margin=_detail_str_field(dj, "margin"),
        mgn_ratio=_detail_str_field(dj, "mgnRatio"),
        liq_px=_detail_str_field(dj, "liqPx"),
        c_time=r.c_time,
        detail_json=dj,
        created_at=r.created_at,
    )


def _row_pick_str(row: dict, keys: list[str]) -> str | None:
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return None


def _row_pick_ms(row: dict, keys: list[str]) -> int | None:
    for k in keys:
        v = row.get(k)
        if v is None:
            continue
        try:
            n = int(str(v).strip())
            if n > 0:
                return n
        except Exception:
            continue
    return None


def _base_ccy_from_inst_like(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().upper()
    if not s:
        return None
    parts = s.split("-")
    if not parts:
        return None
    base = parts[0].strip()
    return base or None


@router.get("/position-history-events", response_model=PositionEventPageOut)
async def list_position_history_events(
    unique_name: str = Query(..., min_length=1, max_length=128, description="跟单帐户 uniqueName"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> PositionEventPageOut:
    """
    从社区历史仓位接口读取记录，并映射为前端跟单记录使用的事件结构。
    """
    ensure_mysql_db_configured()
    un = unique_name.strip()
    acc = db.execute(select(FollowAccount).where(FollowAccount.unique_name == un)).scalar_one_or_none()
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    rows = await OkxTrade.get_position_history(un, limit=limit, offset=offset)
    items: list[PositionEventOut] = []
    now_dt = now_cn()
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        ms = _row_pick_ms(row, ["uTime", "cTime", "closeTime", "ts"])
        created_at = datetime.fromtimestamp(ms / 1000, tz=now_dt.tzinfo) if ms else now_dt

        pos_id = _row_pick_str(row, ["posId", "positionId", "id"])
        pos_ccy = (
            _row_pick_str(row, ["posCcy", "ccy", "baseCcy", "base"])
            or _base_ccy_from_inst_like(_row_pick_str(row, ["instId", "instFamily", "uly"]))
        )
        pos_side = _row_pick_str(row, ["posSide", "side", "direction"])
        lever = _row_pick_str(row, ["lever", "leverage", "posLever"])
        avg_px = _row_pick_str(row, ["avgPx", "openAvgPx", "openPx"])
        last_px = _row_pick_str(row, ["closeAvgPx", "closePx", "last", "markPx"])
        upl_ratio = _row_pick_str(row, ["uplRatio", "pnlRatio"])
        upl = _row_pick_str(row, ["upl", "realizedPnl", "pnl"])
        pos = _row_pick_str(row, ["pos", "closeSz", "sz"])
        notional_usd = _row_pick_str(
            row,
            ["notionalUsd", "notional_usd", "closeNotionalUsd", "closeNotional", "notional"],
        )
        notional_ccy = _row_pick_str(row, ["notionalCcy", "notional_ccy"])
        notional = _row_pick_str(row, ["notional", "closeNotional"])
        margin = _row_pick_str(row, ["margin", "imr"])
        mgn_ratio = _row_pick_str(row, ["mgnRatio"])
        liq_px = _row_pick_str(row, ["liqPx"])
        c_time = _row_pick_str(row, ["cTime", "closeTime", "uTime", "ts"])
        detail_json = json.dumps(row, ensure_ascii=False)

        items.append(
            PositionEventOut(
                id=-(offset + i + 1),
                follow_account_id=acc.id,
                unique_name=un,
                event_type="close",
                pos_id=pos_id,
                pos_ccy=pos_ccy,
                pos_side=pos_side,
                lever=lever,
                avg_px=avg_px,
                last_px=last_px,
                upl_ratio=upl_ratio,
                upl=upl,
                pos=pos,
                notional_usd=notional_usd,
                notional_ccy=notional_ccy,
                notional=notional,
                margin=margin,
                mgn_ratio=mgn_ratio,
                liq_px=liq_px,
                c_time=c_time,
                detail_json=detail_json,
                created_at=created_at,
            )
        )

    total = offset + len(items)
    return PositionEventPageOut(items=items, total=total)


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


@router.get("/overview-data")
async def get_overview_data(
    unique_name: str = Query(..., min_length=1, max_length=128, description="跟单帐户 uniqueName"),
) -> dict:
    """从交易员公开页 HTML 解析 overviewData；equity 即资产余额。"""
    ensure_mysql_db_configured()
    un = unique_name.strip()
    d = await OkxTrade.get_overview_data(un)
    equity = d.get("equity") if isinstance(d, dict) else None
    return {
        "unique_name": un,
        "overview_data": d if isinstance(d, dict) else {},
        "equity": None if equity is None else str(equity),
    }


@router.get("/linked-okx/positions")
async def linked_okx_positions(
    unique_name: str = Query(..., min_length=1, max_length=128, description="跟单帐户 uniqueName"),
    inst_type: str = Query("SWAP"),
    db: Session = Depends(get_db),
) -> dict:
    """本人持仓（欧易 GET /api/v5/account/positions）；OkxFollowOrderClient.get_positions_inst。"""
    client = _require_linked_okx_client(db, unique_name)
    ok, data = await client.get_positions_inst(inst_type)
    if not ok:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=data)
    return data  # type: ignore[return-value]


@router.get("/linked-okx/account-balance")
async def linked_okx_account_balance(
    unique_name: str = Query(..., min_length=1, max_length=128, description="跟单帐户 uniqueName"),
    ccy: str = Query("USDT", description="币种，默认 USDT"),
    db: Session = Depends(get_db),
) -> dict:
    """本人资产余额（欧易 GET /api/v5/account/balance）。"""
    client = _require_linked_okx_client(db, unique_name)
    ok, data = await client.get_account_balance(ccy=ccy)
    if not ok:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=data)
    return data  # type: ignore[return-value]


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
        open_event_id=r.open_event_id,
        close_event_id=r.close_event_id,
        exit_px=r.exit_px,
        realized_pnl_usdt=r.realized_pnl_usdt,
        unrealized_pnl_usdt=r.unrealized_pnl_usdt,
        last_mark_px=r.last_mark_px,
        src_pos=r.src_pos,
        src_margin=r.src_margin,
        src_mgn_ratio=r.src_mgn_ratio,
        src_liq_px=r.src_liq_px,
        add_position_count=int(r.add_position_count or 0),
        reduce_position_count=int(r.reduce_position_count or 0),
        add_margin_count=int(r.add_margin_count or 0),
        total_invested_usdt=Decimal(str(r.total_invested_usdt or 0)),
        live_last_error=r.live_last_error,
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


@router.delete(
    "/follow-sim-records/{record_id}",
    response_model=FollowSimRecordDeleteOut,
)
def delete_follow_sim_record(
    record_id: int,
    unique_name: str = Query(..., min_length=1, max_length=128, description="跟单帐户 uniqueName"),
    db: Session = Depends(get_db),
) -> FollowSimRecordDeleteOut:
    """删除一条模拟跟单资金记录（follow_sim_records）；与是否启用真实交易无关。"""
    ensure_mysql_db_configured()
    un = unique_name.strip()
    acc = (
        db.execute(select(FollowAccount).where(FollowAccount.unique_name == un))
        .scalar_one_or_none()
    )
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    rec = db.get(FollowSimRecord, record_id)
    if rec is None or rec.follow_account_id != acc.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    db.delete(rec)
    db.commit()
    return FollowSimRecordDeleteOut(id=record_id)


@router.post("/position-action")
async def post_position_action(
    body: PositionActionBody,
    db: Session = Depends(get_db),
) -> dict:
    """持仓操作：加仓 / 减仓 / 平仓 / 反手（按跟单配置下注金额）。"""
    ensure_mysql_db_configured()
    un = body.unique_name.strip()
    acc = (
        db.execute(select(FollowAccount).where(FollowAccount.unique_name == un))
        .scalar_one_or_none()
    )
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not acc.live_trading_enabled:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="未启用真实交易")
    if acc.okx_api_account_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="未绑定 OKX API 帐户")
    bet = (
        Decimal(str(acc.bet_amount_per_position))
        if acc.bet_amount_per_position is not None
        else Decimal(0)
    )
    if bet <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="下注金额需大于 0")

    rec: FollowSimRecord | None = None
    if body.sim_record_id is not None:
        rec = db.get(FollowSimRecord, body.sim_record_id)
        if rec is None or rec.follow_account_id != acc.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    elif body.pos_ccy:
        rec = (
            db.execute(
                select(FollowSimRecord)
                .where(
                    FollowSimRecord.follow_account_id == acc.id,
                    FollowSimRecord.pos_ccy == body.pos_ccy.strip().upper(),
                )
                .order_by(FollowSimRecord.id.desc())
                .limit(1)
            )
            .scalar_one_or_none()
        )
    rec_pos_side = (rec.pos_side if rec else (body.pos_side or "")).strip().lower()
    if rec_pos_side not in ("long", "short"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="无效持仓方向")
    rec_pos_ccy = (rec.pos_ccy if rec else (body.pos_ccy or "")).strip().upper()
    if not rec_pos_ccy:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="缺少币种信息")

    client = require_okx_client(db, acc.okx_api_account_id)
    inst_id = normalize_swap_inst_id(rec_pos_ccy)

    ok_cfg, cfg_data = await client.get_account_config()
    _, cfg_pos_mode = (
        parse_account_config_fields(cfg_data) if ok_cfg else (None, None)
    )
    hedge_mode = cfg_pos_mode != "net_mode"
    td_mode = "isolated"

    ok_pm, pm_data = await client.set_position_mode("long_short_mode")
    if not ok_pm:
        pm_code = str(pm_data.get("code", "")) if isinstance(pm_data, dict) else ""
        if pm_code != "59000":
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=pm_data)

    ok_pos, pos_payload = await client.get_positions_inst("SWAP")
    if not ok_pos:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=pos_payload)
    base_ccy = inst_id.split("-")[0] if "-" in inst_id else inst_id

    def _row_ok_for_target(
        row: dict,
        *,
        strict_inst: bool,
        prefer_side: str | None,
    ) -> bool:
        if not isinstance(row, dict):
            return False
        row_inst = str(row.get("instId", "")).strip().upper()
        if strict_inst:
            if row_inst != inst_id:
                return False
        else:
            if not row_inst.startswith(f"{base_ccy}-"):
                return False
        try:
            pos_v = float(str(row.get("pos", "")).strip() or "0")
        except ValueError:
            pos_v = 0.0
        if abs(pos_v) < 1e-12:
            return False
        if hedge_mode and prefer_side in ("long", "short"):
            row_side = str(row.get("posSide", "")).strip().lower()
            if row_side in ("long", "short") and row_side != prefer_side:
                return False
        return True

    target_row: dict | None = None
    for row in (pos_payload.get("data") or []):
        if _row_ok_for_target(row, strict_inst=True, prefer_side=rec_pos_side):
            target_row = row
            break
    if target_row is None:
        for row in (pos_payload.get("data") or []):
            if _row_ok_for_target(row, strict_inst=False, prefer_side=rec_pos_side):
                target_row = row
                inst_id = str(row.get("instId", "")).strip().upper() or inst_id
                break
    # 记录方向可能已过时（例如手工反手后），再做一轮不限方向兜底
    if target_row is None:
        for row in (pos_payload.get("data") or []):
            if _row_ok_for_target(row, strict_inst=True, prefer_side=None):
                target_row = row
                break
    if target_row is None:
        for row in (pos_payload.get("data") or []):
            if _row_ok_for_target(row, strict_inst=False, prefer_side=None):
                target_row = row
                inst_id = str(row.get("instId", "")).strip().upper() or inst_id
                break

    if target_row is None:
        rows = pos_payload.get("data") or []
        same_base_rows: list[dict[str, str]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_inst = str(row.get("instId", "")).strip().upper()
            if not row_inst.startswith(f"{base_ccy}-"):
                continue
            same_base_rows.append(
                {
                    "instId": row_inst,
                    "posSide": str(row.get("posSide", "")).strip().lower(),
                    "mgnMode": str(row.get("mgnMode", "")).strip().lower(),
                    "pos": str(row.get("pos", "")).strip(),
                }
            )
        print(
            f"[position_action] no target follow_id={acc.id} sim_id={getattr(rec, 'id', None)} "
            f"base={base_ccy} prefer_side={rec_pos_side} rows={same_base_rows!r}"
        )
        if body.action != "add":
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "msg": "未找到当前持仓（请确认该币种仍有仓位并刷新页面）",
                    "base_ccy": base_ccy,
                    "prefer_pos_side": rec_pos_side,
                    "rows_same_base": same_base_rows,
                },
            )
        # 无当前仓位时，「加仓」等价为按记录方向开仓
        pos_side = rec_pos_side
        api_pos_side = pos_side if hedge_mode else None
        td_mode = "isolated"
    else:
        row_pos_side = str(target_row.get("posSide", "")).strip().lower()
        pos_side = row_pos_side if row_pos_side in ("long", "short") else rec_pos_side
        api_pos_side = pos_side if hedge_mode else None
        row_mgn_mode = str(target_row.get("mgnMode", "")).strip().lower()
        td_mode = "cross" if row_mgn_mode == "cross" else "isolated"

    lever_i: int | None = None
    if target_row is not None:
        try:
            lever_i = int(float(str(target_row.get("lever", "")).strip()))
        except ValueError:
            lever_i = None
    if lever_i is None:
        ok_li, li_data = await client.get_leverage_info(inst_id, td_mode)
        if ok_li:
            picked = sizing_lever_from_leverage_info(
                li_data,
                hedge_mode=hedge_mode,
                pos_side=pos_side,
            )
            lever_i = picked
    if lever_i is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="无法确定杠杆，请稍后再试")

    principal_s = format(bet, "f").rstrip("0").rstrip(".")
    if not principal_s:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="下注金额无效")

    async def _place_open_with_side(open_side: str) -> tuple[bool, object]:
        side = "buy" if open_side == "long" else "sell"
        return await client.place_swap_market_by_principal_usdt(
            inst_id,
            principal_s,
            leverage=lever_i,
            td_mode=td_mode,
            side=side,
            pos_side=open_side if hedge_mode else None,
        )

    async def _place_reduce_for_side(current_side: str) -> tuple[bool, object]:
        side = "sell" if current_side == "long" else "buy"
        reduce_pos_side = current_side if hedge_mode else None
        return await client.place_swap_market_by_principal_usdt(
            inst_id,
            principal_s,
            leverage=lever_i,
            td_mode=td_mode,
            side=side,
            pos_side=reduce_pos_side,
        )

    def _manual_close_sim_record(*, close_row: dict | None) -> None:
        row = close_row if isinstance(close_row, dict) else {}
        source_pid = rec.pos_id

        src_row: dict | None = None
        snap = db.get(FollowPositionSnapshot, acc.id)
        if snap is not None and snap.snapshot_json:
            try:
                snap_map = json.loads(snap.snapshot_json)
                if isinstance(snap_map, dict):
                    maybe = snap_map.get(source_pid)
                    if isinstance(maybe, dict):
                        src_row = maybe
            except Exception:
                src_row = None

        event_row = src_row or {}
        exit_px = (
            str(row.get("last", "")).strip()
            or str(row.get("avgPx", "")).strip()
            or str(event_row.get("last", "")).strip()
            or str(event_row.get("avgPx", "")).strip()
            or rec.last_mark_px
            or rec.entry_avg_px
            or "0"
        )
        ev = FollowPositionEvent(
            follow_account_id=acc.id,
            unique_name=acc.unique_name or "",
            event_type="close",
            pos_id=source_pid,
            pos_ccy=(str(event_row.get("posCcy", "")).strip() or rec.pos_ccy),
            pos_side=(str(event_row.get("posSide", "")).strip() or rec.pos_side),
            lever=(str(event_row.get("lever", "")).strip() or None),
            avg_px=(str(event_row.get("avgPx", "")).strip() or rec.entry_avg_px),
            last_px=exit_px,
            c_time=(str(event_row.get("cTime", "")).strip() or None),
            detail_json=json.dumps(event_row, ensure_ascii=False) if event_row else None,
        )
        db.add(ev)
        db.flush()

        rec.status = "closed"
        rec.realized_pnl_usdt = _sim_pnl_usdt(
            rec.stake_usdt,
            rec.entry_avg_px,
            exit_px,
            rec.pos_side,
        )
        rec.unrealized_pnl_usdt = Decimal(0)
        rec.exit_px = exit_px
        rec.last_mark_px = exit_px
        rec.close_event_id = ev.id
        rec.closed_at = now_cn()

    action_rec = rec
    if action_rec is None:
        now = now_cn()
        action_rec = FollowSimRecord(
            follow_account_id=acc.id,
            pos_id=(str(target_row.get("posId", "")).strip() if isinstance(target_row, dict) else "") or f"manual-{rec_pos_ccy}-{int(now.timestamp())}",
            pos_ccy=rec_pos_ccy,
            pos_side=rec_pos_side,
            entry_avg_px=(str(target_row.get("avgPx", "")).strip() if isinstance(target_row, dict) else None) or None,
            stake_usdt=Decimal(principal_s),
            status="open",
            unrealized_pnl_usdt=Decimal(0),
            total_invested_usdt=Decimal(principal_s),
            add_position_count=0,
            reduce_position_count=0,
            add_margin_count=0,
            opened_at=now,
            updated_at=now,
        )
        db.add(action_rec)
        db.flush()
    if body.action == "add":
        ok_act, payload = await _place_open_with_side(pos_side)
        if not ok_act:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=payload)
        if rec.status == "open":
            rec.add_position_count = int(rec.add_position_count or 0) + 1
            rec.total_invested_usdt = Decimal(str(rec.total_invested_usdt or 0)) + Decimal(principal_s)
        else:
            now = now_cn()
            action_rec = FollowSimRecord(
                follow_account_id=acc.id,
                pos_id=rec.pos_id,
                pos_ccy=rec.pos_ccy,
                pos_side=rec.pos_side,
                entry_avg_px=rec.entry_avg_px,
                stake_usdt=Decimal(principal_s),
                status="open",
                open_event_id=None,
                close_event_id=None,
                exit_px=None,
                realized_pnl_usdt=None,
                unrealized_pnl_usdt=Decimal(0),
                last_mark_px=None,
                src_pos=None,
                src_margin=None,
                src_mgn_ratio=None,
                src_liq_px=None,
                add_position_count=0,
                reduce_position_count=0,
                add_margin_count=0,
                total_invested_usdt=Decimal(principal_s),
                live_open_ok=None,
                live_close_ok=None,
                opened_at=now,
                closed_at=None,
                updated_at=now,
            )
            db.add(action_rec)
    elif body.action == "reduce":
        ok_act, payload = await _place_reduce_for_side(pos_side)
        if not ok_act:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=payload)
        rec.reduce_position_count = int(rec.reduce_position_count or 0) + 1
    elif body.action == "close":
        ok_act, payload = await client.close_swap_position(inst_id, td_mode, api_pos_side)
        if not ok_act:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=payload)
        _manual_close_sim_record(close_row=target_row)
    else:  # reverse
        ok_close, payload_close = await client.close_swap_position(inst_id, td_mode, api_pos_side)
        if not ok_close:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"step": "close", "okx": payload_close})
        _manual_close_sim_record(close_row=target_row)
        if target_row is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="反手失败：未找到原仓位保证金")
        margin_raw = str(target_row.get("margin", "")).strip()
        if margin_raw == "":
            margin_raw = str(target_row.get("imr", "")).strip()
        try:
            principal_val = abs(float(margin_raw))
        except ValueError:
            principal_val = 0.0
        principal_str = f"{principal_val:.8f}".rstrip("0").rstrip(".")
        if not principal_str or principal_str == "0":
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="反手失败：原仓位保证金无效")
        rev_side = "short" if pos_side == "long" else "long"
        ok_open, payload_open = await client.place_swap_market_by_principal_usdt(
            inst_id,
            principal_str,
            leverage=lever_i,
            td_mode=td_mode,
            side="buy" if rev_side == "long" else "sell",
            pos_side=rev_side if hedge_mode else None,
        )
        if not ok_open:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail={"step": "reverse_open", "okx": payload_open})

    action_rec.updated_at = now_cn()
    db.commit()
    db.refresh(action_rec)
    return {
        "ok": True,
        "action": body.action,
        "sim_record_id": action_rec.id,
        "add_position_count": int(action_rec.add_position_count or 0),
        "reduce_position_count": int(action_rec.reduce_position_count or 0),
        "add_margin_count": int(action_rec.add_margin_count or 0),
    }


@router.post("/snapshot-follow")
async def snapshot_follow_once(
    body: SnapshotFollowBody,
    db: Session = Depends(get_db),
) -> dict:
    """按「对方持仓」单条记录执行一次跟单开仓。"""
    ensure_mysql_db_configured()
    un = body.unique_name.strip()
    pid = body.pos_id.strip()
    acc = (
        db.execute(select(FollowAccount).where(FollowAccount.unique_name == un))
        .scalar_one_or_none()
    )
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not acc.live_trading_enabled:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="未启用真实交易")
    if acc.okx_api_account_id is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="未绑定 OKX API 帐户")

    coeff = Decimal(str(acc.open_by_asset_ratio_coeff or 0))
    if coeff <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="持仓量系数需大于 0")

    snap = db.get(FollowPositionSnapshot, acc.id)
    if snap is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到对方持仓快照")
    try:
        snap_map = json.loads(snap.snapshot_json)
    except Exception:
        snap_map = {}
    row = snap_map.get(pid) if isinstance(snap_map, dict) else None
    if not isinstance(row, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到对应持仓")

    ccy = str(row.get("posCcy", "")).strip().upper()
    pos_side = str(row.get("posSide", "")).strip().lower()
    if not ccy or pos_side not in ("long", "short"):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="持仓方向或币种无效")
    inst_id = normalize_swap_inst_id(ccy)

    client = require_okx_client(db, acc.okx_api_account_id)
    ok_cfg, cfg_data = await client.get_account_config()
    _, cfg_pos_mode = parse_account_config_fields(cfg_data) if ok_cfg else (None, None)
    hedge_mode = cfg_pos_mode != "net_mode"
    td_mode = "isolated"

    ok_pm, pm_data = await client.set_position_mode("long_short_mode")
    if not ok_pm:
        pm_code = str(pm_data.get("code", "")) if isinstance(pm_data, dict) else ""
        if pm_code != "59000":
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=pm_data)

    ok_pos, pos_payload = await client.get_positions_inst("SWAP")
    if not ok_pos:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=pos_payload)
    has_existing_position = False
    for p in (pos_payload.get("data") or []):
        if not isinstance(p, dict):
            continue
        if str(p.get("instId", "")).strip().upper() != inst_id:
            continue
        try:
            pv = float(str(p.get("pos", "")).strip() or "0")
        except ValueError:
            pv = 0.0
        if abs(pv) > 1e-12:
            has_existing_position = True
            break

    lever_i: int | None = None
    lv_raw = str(row.get("lever", "")).strip()
    if lv_raw:
        try:
            lever_i = int(float(lv_raw))
        except ValueError:
            lever_i = None
    if lever_i is None:
        ok_li, li_data = await client.get_leverage_info(inst_id, td_mode)
        if ok_li:
            lever_i = sizing_lever_from_leverage_info(li_data, hedge_mode=hedge_mode, pos_side=pos_side)
    if lever_i is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="无法确定杠杆，请稍后再试")

    try:
        src_pos_abs = abs(Decimal(str(row.get("pos") or "0").strip()))
    except Exception:
        src_pos_abs = Decimal(0)
    if src_pos_abs <= 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="对方持仓张数无效")
    contracts_s = format((src_pos_abs * coeff), "f").rstrip("0").rstrip(".")
    if not contracts_s:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="按持仓量系数计算后张数无效")

    latest = (
        db.execute(
            select(FollowSimRecord)
            .where(
                FollowSimRecord.follow_account_id == acc.id,
                FollowSimRecord.pos_id == pid,
            )
            .order_by(FollowSimRecord.id.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )
    if latest is not None and latest.status == "open":
        latest.live_close_ok = None
        latest.updated_at = now_cn()
        db.commit()
        db.refresh(latest)
        return {"ok": True, "sim_record_id": latest.id, "already_open": True}

    if not has_existing_position:
        ok_open, payload_open = await client.place_swap_market_by_sz(
            inst_id,
            contracts_s,
            td_mode=td_mode,
            side="buy" if pos_side == "long" else "sell",
            pos_side=pos_side if hedge_mode else None,
        )
        if not ok_open:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=payload_open)

    now = now_cn()
    rec = FollowSimRecord(
        follow_account_id=acc.id,
        pos_id=pid,
        pos_ccy=ccy,
        pos_side=pos_side,
        entry_avg_px=(str(row.get("avgPx", "")).strip() or None),
        stake_usdt=Decimal(str(acc.bet_amount_per_position or 0)),
        status="open",
        open_event_id=None,
        close_event_id=None,
        exit_px=None,
        realized_pnl_usdt=None,
        unrealized_pnl_usdt=Decimal(0),
        last_mark_px=(str(row.get("last", "")).strip() or None),
        src_pos=(str(row.get("pos", "")).strip() or None),
        src_margin=(str(row.get("margin", "")).strip() or None),
        src_mgn_ratio=(str(row.get("mgnRatio", "")).strip() or None),
        src_liq_px=(str(row.get("liqPx", "")).strip() or None),
        add_position_count=0,
        reduce_position_count=0,
        add_margin_count=0,
        total_invested_usdt=Decimal(str(acc.bet_amount_per_position or 0)),
        live_open_ok=True,
        live_close_ok=None,
        opened_at=now,
        closed_at=None,
        updated_at=now,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return {"ok": True, "sim_record_id": rec.id, "already_holding": has_existing_position}


@router.post("/snapshot-follow-stop")
async def snapshot_follow_stop_once(
    body: SnapshotFollowBody,
    db: Session = Depends(get_db),
) -> dict:
    """按「对方持仓」单条记录关闭跟单：仅写关闭标记，不自动平仓。"""
    ensure_mysql_db_configured()
    un = body.unique_name.strip()
    pid = body.pos_id.strip()
    acc = (
        db.execute(select(FollowAccount).where(FollowAccount.unique_name == un))
        .scalar_one_or_none()
    )
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    snap = db.get(FollowPositionSnapshot, acc.id)
    if snap is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到对方持仓快照")
    try:
        snap_map = json.loads(snap.snapshot_json)
    except Exception:
        snap_map = {}
    row = snap_map.get(pid) if isinstance(snap_map, dict) else None
    if not isinstance(row, dict):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="未找到对应持仓")

    latest = (
        db.execute(
            select(FollowSimRecord)
            .where(
                FollowSimRecord.follow_account_id == acc.id,
                FollowSimRecord.pos_id == pid,
            )
            .order_by(FollowSimRecord.id.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )

    exit_px = (
        str(row.get("last", "")).strip()
        or str(row.get("avgPx", "")).strip()
        or (latest.entry_avg_px if latest else None)
        or "0"
    )
    close_ev = FollowPositionEvent(
        follow_account_id=acc.id,
        unique_name=acc.unique_name or "",
        event_type="close",
        pos_id=pid,
        pos_ccy=(str(row.get("posCcy", "")).strip() or (latest.pos_ccy if latest else None)),
        pos_side=(str(row.get("posSide", "")).strip() or (latest.pos_side if latest else None)),
        lever=(str(row.get("lever", "")).strip() or None),
        avg_px=(str(row.get("avgPx", "")).strip() or (latest.entry_avg_px if latest else None)),
        last_px=exit_px,
        c_time=(str(row.get("cTime", "")).strip() or None),
        detail_json=json.dumps(row, ensure_ascii=False),
    )
    db.add(close_ev)
    db.flush()

    if latest is not None and latest.status == "open":
        latest.status = "closed"
        latest.realized_pnl_usdt = _sim_pnl_usdt(
            latest.stake_usdt,
            latest.entry_avg_px,
            exit_px,
            latest.pos_side,
        )
        latest.unrealized_pnl_usdt = Decimal(0)
        latest.exit_px = exit_px
        latest.last_mark_px = exit_px
        latest.close_event_id = close_ev.id
        # 显式标记为「手动关闭跟单」，用于后续默认开启判定。
        latest.live_close_ok = True
        latest.closed_at = now_cn()
        latest.updated_at = now_cn()
        target_rec_id = latest.id
    else:
        now = now_cn()
        marker = FollowSimRecord(
            follow_account_id=acc.id,
            pos_id=pid,
            pos_ccy=(str(row.get("posCcy", "")).strip() or None),
            pos_side=(str(row.get("posSide", "")).strip() or None),
            entry_avg_px=(str(row.get("avgPx", "")).strip() or None),
            stake_usdt=Decimal(0),
            status="closed",
            open_event_id=None,
            close_event_id=close_ev.id,
            exit_px=exit_px,
            realized_pnl_usdt=Decimal(0),
            unrealized_pnl_usdt=Decimal(0),
            last_mark_px=exit_px,
            src_pos=(str(row.get("pos", "")).strip() or None),
            src_margin=(str(row.get("margin", "")).strip() or None),
            src_mgn_ratio=(str(row.get("mgnRatio", "")).strip() or None),
            src_liq_px=(str(row.get("liqPx", "")).strip() or None),
            add_position_count=0,
            reduce_position_count=0,
            add_margin_count=0,
            total_invested_usdt=Decimal(0),
            live_open_ok=False,
            live_close_ok=True,
            opened_at=now,
            closed_at=now,
            updated_at=now,
        )
        db.add(marker)
        db.flush()
        target_rec_id = marker.id

    db.commit()
    return {"ok": True, "sim_record_id": target_rec_id}


@router.post("/snapshot-follow-side-stop")
async def snapshot_follow_side_stop_once(
    body: SnapshotFollowSideBody,
    db: Session = Depends(get_db),
) -> dict:
    """按币种+方向关闭跟单（不自动平仓）。"""
    ensure_mysql_db_configured()
    un = body.unique_name.strip()
    ccy = body.pos_ccy.strip().upper()
    side = body.pos_side.strip().lower()
    acc = (
        db.execute(select(FollowAccount).where(FollowAccount.unique_name == un))
        .scalar_one_or_none()
    )
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    pid = f"__side_block__:{ccy}:{side}"
    now = now_cn()
    rec = FollowSimRecord(
        follow_account_id=acc.id,
        pos_id=pid,
        pos_ccy=ccy,
        pos_side=side,
        entry_avg_px=None,
        stake_usdt=Decimal(0),
        status="closed",
        open_event_id=None,
        close_event_id=None,
        exit_px=None,
        realized_pnl_usdt=Decimal(0),
        unrealized_pnl_usdt=Decimal(0),
        last_mark_px=None,
        src_pos=None,
        src_margin=None,
        src_mgn_ratio=None,
        src_liq_px=None,
        add_position_count=0,
        reduce_position_count=0,
        add_margin_count=0,
        total_invested_usdt=Decimal(0),
        live_open_ok=False,
        live_close_ok=True,
        opened_at=now,
        closed_at=now,
        updated_at=now,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return {"ok": True, "sim_record_id": rec.id}


@router.post("/snapshot-follow-side-enable")
async def snapshot_follow_side_enable_once(
    body: SnapshotFollowSideBody,
    db: Session = Depends(get_db),
) -> dict:
    """按币种+方向恢复跟单（删除暂停配置标记）。"""
    ensure_mysql_db_configured()
    un = body.unique_name.strip()
    ccy = body.pos_ccy.strip().upper()
    side = body.pos_side.strip().lower()
    acc = (
        db.execute(select(FollowAccount).where(FollowAccount.unique_name == un))
        .scalar_one_or_none()
    )
    if acc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    pid = f"__side_block__:{ccy}:{side}"
    rows = (
        db.execute(
            select(FollowSimRecord).where(
                FollowSimRecord.follow_account_id == acc.id,
                FollowSimRecord.pos_id == pid,
            )
        )
        .scalars()
        .all()
    )
    deleted_ids: list[int] = []
    for rec in rows:
        deleted_ids.append(int(rec.id))
        db.delete(rec)
    db.commit()
    return {"ok": True, "deleted_count": len(deleted_ids), "deleted_ids": deleted_ids}


@router.patch("/{account_id}/okx-bind", response_model=FollowAccountOut)
def patch_follow_okx_bind(
    account_id: int,
    payload: FollowAccountOkxBindPatch,
    db: Session = Depends(get_db),
) -> FollowAccountOut:
    """绑定/更换 OKX API 帐户；已启用跟单时不允许置为未绑定。"""
    ensure_mysql_db_configured()
    row = (
        db.execute(select(FollowAccount).where(FollowAccount.id == account_id))
        .scalar_one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    new_id = payload.okx_api_account_id
    if new_id is not None:
        cred = db.get(OkxApiAccount, new_id)
        if cred is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OKX API 帐户不存在")
        other = (
            db.execute(
                select(FollowAccount).where(
                    FollowAccount.okx_api_account_id == new_id,
                    FollowAccount.id != account_id,
                )
            )
            .scalar_one_or_none()
        )
        if other is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="该 API 帐户已绑定其他交易员，一个 API 帐户仅能绑定一个交易员",
            )
    if new_id is None and row.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="跟单已启用时不能解除 API 绑定，请先停用跟单",
        )

    row.okx_api_account_id = new_id
    db.commit()
    db.refresh(row)
    return _to_out(row, positions_refreshed_at=_snapshot_refreshed_at(db, row.id))


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
    if row.okx_api_account_id is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="启用真实交易前请先绑定跟单帐户（OKX API）",
        )
    # 跟单配置固定策略：直接启用真实交易；系数按“持仓量系数”写入历史列 open_by_asset_ratio_coeff。
    single_add_margin_usdt = data.pop("single_add_margin_usdt", None)
    data.pop("live_trading_enabled", None)
    data.pop("open_by_asset_ratio", None)
    coeff = data.pop("position_size_coeff", None)
    data.pop("open_by_asset_ratio_coeff", None)
    data.pop("margin_auto_enabled", None)
    data.pop("margin_add_ratio_of_bet", None)
    if single_add_margin_usdt is not None:
        row.bet_amount_per_position = Decimal(str(single_add_margin_usdt))
    row.live_trading_enabled = True
    row.margin_auto_enabled = True
    row.margin_add_ratio_of_bet = Decimal("1")
    row.open_by_asset_ratio = False
    if coeff is not None:
        row.open_by_asset_ratio_coeff = Decimal(str(coeff))
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

    if payload.enabled and row.okx_api_account_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="启用跟单前请先在详情页绑定 OKX API 帐户",
        )

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
