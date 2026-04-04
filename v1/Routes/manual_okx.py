"""
手动合约交易与追加保证金：仅转发 OKX，不落库；记录列表由前端调 OKX 代理接口展示。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from module.follow_order import OkxFollowOrderClient, follow_order_config
from v1.Models.user import User
from v1.Routes.auth import get_current_user

router = APIRouter(prefix="/manual-okx", tags=["manual-okx"])
_client = OkxFollowOrderClient()


def _ensure_okx() -> None:
    if not follow_order_config.is_configured():
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="OKX_FOLLOW_API_KEY / SECRET / PASSPHRASE 未配置",
        )


def normalize_swap_inst_id(raw: str) -> str:
    s = raw.strip().upper()
    if not s:
        return s
    if "-" in s:
        return s
    return f"{s}-USDT-SWAP"


class ContractOrderBody(BaseModel):
    """市价开仓；开多 side=buy+posSide=long，开空 side=sell+posSide=short。"""

    symbol: str = Field(..., min_length=1, max_length=64, description="如 BTC 或 BTC-USDT-SWAP")
    sz: str = Field(..., min_length=1, max_length=32, description="委托数量，U 本位永续一般为张数")
    pos_side: str = Field(..., pattern="^(long|short)$")
    td_mode: str = Field(default="isolated", pattern="^(isolated|cross)$")


class MarginAddBody(BaseModel):
    inst_id: str = Field(..., min_length=1, max_length=64)
    pos_side: str = Field(..., pattern="^(long|short|net)$")
    amt: str = Field(..., min_length=1, max_length=32)


@router.post("/contract-order")
async def post_contract_order(
    body: ContractOrderBody,
    _: User = Depends(get_current_user),
) -> dict:
    _ensure_okx()
    inst_id = normalize_swap_inst_id(body.symbol)
    side = "buy" if body.pos_side == "long" else "sell"
    ok, data = await _client.place_order(
        {
            "instId": inst_id,
            "tdMode": body.td_mode,
            "side": side,
            "ordType": "market",
            "sz": body.sz.strip(),
            "posSide": body.pos_side,
        }
    )
    if not ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=data)
    return data  # type: ignore[return-value]


@router.post("/margin-add")
async def post_margin_add(
    body: MarginAddBody,
    _: User = Depends(get_current_user),
) -> dict:
    _ensure_okx()
    inst_id = normalize_swap_inst_id(body.inst_id)
    ok, data = await _client.add_position_margin(
        inst_id,
        body.pos_side.lower(),
        body.amt.strip(),
    )
    if not ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=data)
    return data  # type: ignore[return-value]


@router.get("/fills")
async def get_okx_fills(
    _: User = Depends(get_current_user),
    inst_type: str = Query("SWAP"),
    inst_id: str | None = Query(None, description="可选，仅看某一交易对"),
    limit: int = Query(50, ge=1, le=100),
) -> dict:
    """代理 GET /api/v5/trade/fills，供前端展示成交记录。"""
    _ensure_okx()
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
    _: User = Depends(get_current_user),
    inst_type: str = Query("SWAP"),
    limit: int = Query(100, ge=1, le=100),
) -> dict:
    """代理账单 type=6（保证金划转），供前端展示追加/减少保证金相关流水。"""
    _ensure_okx()
    ok, data = await _client.get_margin_transfer_bills(inst_type=inst_type, limit=limit)
    if not ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail=data)
    return data  # type: ignore[return-value]
