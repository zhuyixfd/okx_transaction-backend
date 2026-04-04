"""
手动合约交易与追加保证金：仅转发 OKX，不落库；记录列表由前端调 OKX 代理接口展示。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

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
    """统一为 U 本位永续 instId（*-USDT-SWAP）。仅「币-USDT」两段时补 -SWAP，完整 instId 保持原样。"""
    s = raw.strip().upper()
    if not s:
        return s
    if s.endswith("-SWAP"):
        return s
    if "-" not in s:
        return f"{s}-USDT-SWAP"
    parts = s.split("-")
    if len(parts) == 2:
        return f"{s}-SWAP"
    return s


# OKX 文档：逐仓 isolated 在跨币种(3)、组合保证金(4)账户下不可用，否则会报 Parameter mgnMode 类错误。
_ACCT_LV_NO_ISOLATED = frozenset({"3", "4"})


def _effective_td_mode_for_account(requested: str, acct_lv: str | None) -> str:
    if requested == "isolated" and acct_lv in _ACCT_LV_NO_ISOLATED:
        return "cross"
    return requested


def _parse_account_config_fields(cfg_data: Any) -> tuple[str | None, str | None]:
    """返回 (acctLv, posMode)。posMode 为 net_mode 时下单/设杠杆不得传 long/short 的 posSide。"""
    if not isinstance(cfg_data, dict) or str(cfg_data.get("code")) != "0":
        return None, None
    rows = cfg_data.get("data")
    if not isinstance(rows, list) or not rows:
        return None, None
    first = rows[0]
    if not isinstance(first, dict):
        return None, None
    lv = first.get("acctLv")
    acct_lv = str(lv) if lv is not None and str(lv) != "" else None
    pm = first.get("posMode")
    pos_mode = str(pm).strip() if pm is not None and str(pm).strip() != "" else None
    return acct_lv, pos_mode


def _sizing_lever_from_leverage_info(
    li_data: Any,
    *,
    hedge_mode: bool,
    pos_side: str,
) -> int | None:
    """从 leverage-info 响应中取杠杆（开平仓优先匹配 posSide）。"""
    if not isinstance(li_data, dict) or str(li_data.get("code")) != "0":
        return None
    rows = li_data.get("data")
    if not isinstance(rows, list) or not rows:
        return None

    def row_lever(r: object) -> int | None:
        if not isinstance(r, dict):
            return None
        v = r.get("lever")
        if v is None or str(v).strip() == "":
            return None
        try:
            x = int(float(str(v).strip()))
        except ValueError:
            return None
        return x if x >= 1 else None

    if hedge_mode:
        for r in rows:
            if isinstance(r, dict) and r.get("posSide") == pos_side:
                lv = row_lever(r)
                if lv is not None:
                    return lv
    for r in rows:
        if isinstance(r, dict) and r.get("posSide") == "net":
            lv = row_lever(r)
            if lv is not None:
                return lv
    for r in rows:
        lv = row_lever(r)
        if lv is not None:
            return lv
    return None


class ContractOrderBody(BaseModel):
    """市价开仓；根据账户 posMode 自动选择是否带 posSide（开平仓 long/short，单向 net 不传）。"""

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
    td_mode: str = Field(default="isolated", pattern="^(isolated|cross)$")
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
    inst_id: str = Field(..., min_length=1, max_length=64)
    pos_side: str = Field(..., pattern="^(long|short)$")
    amt: str = Field(..., min_length=1, max_length=32)


@router.post("/contract-order")
async def post_contract_order(
    body: ContractOrderBody,
    _: User = Depends(get_current_user),
) -> dict:
    _ensure_okx()
    inst_id = normalize_swap_inst_id(body.symbol)
    side = "buy" if body.direction == "long" else "sell"
    pos_side = "long" if body.direction == "long" else "short"

    ok_cfg, cfg_data = await _client.get_account_config()
    acct_lv, cfg_pos_mode = (
        _parse_account_config_fields(cfg_data) if ok_cfg else (None, None)
    )
    td_mode = _effective_td_mode_for_account(body.td_mode, acct_lv)
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
        lev_pos: str | None = (
            pos_side if td_mode == "isolated" and hedge_mode else None
        )
        # set-leverage：若传 ccy 则 mgnMode 只能为 cross（官方表）；逐仓永续示例不含 ccy。
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
        picked = _sizing_lever_from_leverage_info(
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
    _: User = Depends(get_current_user),
) -> dict:
    _ensure_okx()
    inst_id = normalize_swap_inst_id(body.inst_id)
    ok_cfg, cfg_data = await _client.get_account_config()
    _, cfg_pos_mode = (
        _parse_account_config_fields(cfg_data) if ok_cfg else (None, None)
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
