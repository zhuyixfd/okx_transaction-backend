"""
手动合约交易与追加保证金：仅转发 OKX，不落库；记录列表由前端调 OKX 代理接口展示。
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from config.db import get_db
from module.follow_order import OkxFollowOrderClient
from v1.Models.user import User
from v1.Routes.auth import get_current_user
from v1.Services.okx_account_client import require_okx_client
from v1.Services.okx_contract_helpers import (
    isolated_td_mode_blocked_reason,
    normalize_swap_inst_id,
    parse_account_config_fields,
    sizing_lever_from_leverage_info,
)

router = APIRouter(prefix="/manual-okx", tags=["manual-okx"])


class ContractOrderBody(BaseModel):
    """市价开仓；保证金固定逐仓 isolated。根据账户 posMode 自动选择是否带 posSide（开平仓 long/short，单向 net 不传）。"""

    okx_api_account_id: int = Field(..., ge=1, description="数据库 okx_api_accounts.id")
    symbol: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="币种简称如 DOGE，或无横杠则自动补全为 {SYMBOL}-USDT-SWAP",
    )
    principal_usdt: str = Field(
        ...,
        min_length=1,
        max_length=32,
        description="开仓保证金/本金（USDT）",
    )
    direction: Literal["long", "short"] = Field(
        ...,
        description="做多 buy+long，做空 sell+short",
    )
    lever: str | None = Field(
        default=None,
        max_length=16,
        description="杠杆倍数；填写则先 set-leverage。不填则读当前合约杠杆",
    )

    @field_validator("lever", mode="before")
    @classmethod
    def empty_lever_to_none(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None


class MarginAddBody(BaseModel):
    okx_api_account_id: int = Field(..., ge=1, description="数据库 okx_api_accounts.id")
    inst_id: str = Field(..., min_length=1, max_length=64)
    pos_side: str = Field(..., pattern="^(long|short)$")
    amt: str = Field(..., min_length=1, max_length=32)


@router.post("/contract-order")
async def post_contract_order(
    body: ContractOrderBody,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    _client = require_okx_client(db, body.okx_api_account_id)
    inst_id = normalize_swap_inst_id(body.symbol)
    side = "buy" if body.direction == "long" else "sell"
    pos_side = "long" if body.direction == "long" else "short"

    ok_cfg, cfg_data = await _client.get_account_config()
    acct_lv, cfg_pos_mode = (
        parse_account_config_fields(cfg_data) if ok_cfg else (None, None)
    )
    blocked = isolated_td_mode_blocked_reason(acct_lv)
    if blocked:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=blocked)
    td_mode = "isolated"
    # 以 config 为准：单向 net_mode 下传 long/short 的 posSide 会触发 Parameter mgnMode 等错误
    hedge_mode = cfg_pos_mode != "net_mode"

    ok_pm, pm_data = await _client.set_position_mode("long_short_mode")
    # 59000：有挂单/持仓/机器人时 OKX 拒绝改持仓模式；若已是开平仓模式仍可下单，故不阻断。
    if not ok_pm:
        pm_code = str(pm_data.get("code", "")) if isinstance(pm_data, dict) else ""
        if pm_code != "59000":
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=pm_data)

    sizing_lever: int
    if body.lever is not None:
        try:
            lv = int(body.lever)
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="lever 须为正整数",
            )
        if not (1 <= lv <= 125):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="lever 须在 1～125 之间",
            )
        sizing_lever = lv
        lev_pos: str | None = pos_side if hedge_mode else None
        # set-leverage：逐仓永续示例不含 ccy；若传 ccy 则 mgnMode 只能为 cross（官方表）。
        ok_lev, lev_data = await _client.set_leverage(
            inst_id,
            str(lv),
            td_mode,
            pos_side=lev_pos,
            ccy=None,
        )
        if not ok_lev:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail={"step": "set_leverage", "okx": lev_data},
            )
    else:
        ok_li, li_data = await _client.get_leverage_info(inst_id, td_mode)
        if not ok_li:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                detail={"step": "get_leverage_info", "okx": li_data},
            )
        picked = sizing_lever_from_leverage_info(
            li_data,
            hedge_mode=hedge_mode,
            pos_side=pos_side,
        )
        if picked is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="无法读取当前合约杠杆，请在本单填写 lever",
            )
        sizing_lever = picked

    ok_order, payload = await _client.place_swap_market_by_principal_usdt(
        inst_id,
        body.principal_usdt.strip(),
        leverage=sizing_lever,
        td_mode=td_mode,
        side=side,
        pos_side=pos_side if hedge_mode else None,
    )
    if not ok_order:
        kind, detail = payload  # type: ignore[misc]
        if kind == "sz":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=detail if isinstance(detail, dict) else {"msg": str(detail)},
            )
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            detail={"step": "place_order", "okx": detail},
        )
    return payload  # type: ignore[return-value]


@router.post("/margin-add")
async def post_margin_add(
    body: MarginAddBody,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    _client = require_okx_client(db, body.okx_api_account_id)
    inst_id = normalize_swap_inst_id(body.inst_id)
    ok_cfg, cfg_data = await _client.get_account_config()
    _, cfg_pos_mode = (
        parse_account_config_fields(cfg_data) if ok_cfg else (None, None)
    )
    # 买卖模式（单向）下 OKX 要求 posSide=net，与多/空无关
    api_pos_side = (
        "net"
        if cfg_pos_mode == "net_mode"
        else body.pos_side.lower()
    )
    ok, data = await _client.add_position_margin(
        inst_id,
        api_pos_side,
        body.amt.strip(),
    )
    if not ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=data)
    return data  # type: ignore[return-value]


@router.get("/fills")
async def get_okx_fills(
    okx_api_account_id: int = Query(..., ge=1, description="数据库 okx_api_accounts.id"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    inst_type: str = Query("SWAP"),
    inst_id: str | None = Query(None, description="可选，仅看某一交易对"),
    limit: int = Query(50, ge=1, le=100),
) -> dict:
    """代理 GET /api/v5/trade/fills，供前端展示成交记录。"""
    _client = require_okx_client(db, okx_api_account_id)
    ok, data = await _client.get_trade_fills(
        inst_type=inst_type,
        inst_id=inst_id.strip() if inst_id else None,
        limit=limit,
    )
    if not ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=data)
    return data  # type: ignore[return-value]


@router.get("/margin-bills")
async def get_okx_margin_bills(
    okx_api_account_id: int = Query(..., ge=1, description="数据库 okx_api_accounts.id"),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
    inst_type: str = Query("SWAP"),
    limit: int = Query(100, ge=1, le=100),
) -> dict:
    """代理账单 type=6（保证金划转），供前端展示追加/减少保证金相关流水。"""
    _client = require_okx_client(db, okx_api_account_id)
    ok, data = await _client.get_margin_transfer_bills(inst_type=inst_type, limit=limit)
    if not ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=data)
    return data  # type: ignore[return-value]
