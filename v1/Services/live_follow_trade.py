"""
自动跟单真实开平仓：与 POST /manual-okx/contract-order 相同的账户配置、设杠杆与按保证金市价开仓；
对方平仓且本笔曾在 OKX 开仓成功时，调用 close-position 市价平仓。
"""

from __future__ import annotations

from dataclasses import dataclass

from config.cn_time import now_cn
from config.db import SessionLocal
from module.follow_order import okx_client_for_db_secrets
from v1.Models.follow_sim_record import FollowSimRecord
from v1.Models.okx_api_account import OkxApiAccount
from v1.Services.okx_contract_helpers import (
    isolated_td_mode_blocked_reason,
    parse_account_config_fields,
    sizing_lever_from_leverage_info,
)

LIVE_FOLLOW_TD_MODE = "isolated"


@dataclass(frozen=True)
class LiveFollowOpenIntent:
    follow_account_id: int
    okx_api_account_id: int
    sim_record_id: int
    pos_id: str
    inst_id: str
    pos_side: str
    lever_str: str | None
    principal_usdt: str


@dataclass(frozen=True)
class LiveFollowCloseIntent:
    follow_account_id: int
    okx_api_account_id: int
    sim_record_id: int
    inst_id: str
    pos_side: str | None


def _set_live_open_ok(sim_id: int, value: bool) -> None:
    db = SessionLocal()
    try:
        r = db.get(FollowSimRecord, sim_id)
        if r:
            r.live_open_ok = value
            r.updated_at = now_cn()
            db.commit()
    finally:
        db.close()


def _set_live_close_ok(sim_id: int, value: bool) -> None:
    db = SessionLocal()
    try:
        r = db.get(FollowSimRecord, sim_id)
        if r:
            r.live_close_ok = value
            r.updated_at = now_cn()
            db.commit()
    finally:
        db.close()


async def execute_live_follow_open(intent: LiveFollowOpenIntent) -> None:
    db = SessionLocal()
    try:
        cred = db.get(OkxApiAccount, intent.okx_api_account_id)
        if cred is None:
            print(
                f"[live_follow] open skip no cred sim_id={intent.sim_record_id} "
                f"pos_id={intent.pos_id!r}"
            )
            _set_live_open_ok(intent.sim_record_id, False)
            return
        client = okx_client_for_db_secrets(cred.api_key, cred.api_secret, cred.api_passphrase)
        if not client.is_configured():
            print(
                f"[live_follow] open skip unconfigured okx_id={intent.okx_api_account_id} "
                f"sim_id={intent.sim_record_id}"
            )
            _set_live_open_ok(intent.sim_record_id, False)
            return
    finally:
        db.close()

    inst_id = intent.inst_id
    side = "buy" if intent.pos_side == "long" else "sell"
    pos_side = intent.pos_side

    ok_cfg, cfg_data = await client.get_account_config()
    acct_lv, cfg_pos_mode = (
        parse_account_config_fields(cfg_data) if ok_cfg else (None, None)
    )
    blocked = isolated_td_mode_blocked_reason(acct_lv)
    if blocked:
        print(f"[live_follow] open skip sim_id={intent.sim_record_id}: {blocked}")
        _set_live_open_ok(intent.sim_record_id, False)
        return
    td_mode = LIVE_FOLLOW_TD_MODE
    hedge_mode = cfg_pos_mode != "net_mode"

    ok_pm, pm_data = await client.set_position_mode("long_short_mode")
    if not ok_pm:
        pm_code = str(pm_data.get("code", "")) if isinstance(pm_data, dict) else ""
        if pm_code != "59000":
            print(
                f"[live_follow] open set_position_mode fail sim_id={intent.sim_record_id}: "
                f"{pm_data!r}"
            )
            _set_live_open_ok(intent.sim_record_id, False)
            return

    sizing_lever: int
    if intent.lever_str:
        try:
            lv = int(intent.lever_str)
        except ValueError:
            print(
                f"[live_follow] open bad lever sim_id={intent.sim_record_id}: "
                f"{intent.lever_str!r}"
            )
            _set_live_open_ok(intent.sim_record_id, False)
            return
        if not (1 <= lv <= 125):
            print(f"[live_follow] open lever OOB sim_id={intent.sim_record_id}: {lv}")
            _set_live_open_ok(intent.sim_record_id, False)
            return
        sizing_lever = lv
        lev_pos: str | None = pos_side if hedge_mode else None
        ok_lev, lev_data = await client.set_leverage(
            inst_id, str(lv), td_mode, pos_side=lev_pos, ccy=None
        )
        if not ok_lev:
            print(
                f"[live_follow] open set_leverage fail sim_id={intent.sim_record_id}: "
                f"{lev_data!r}"
            )
            _set_live_open_ok(intent.sim_record_id, False)
            return
    else:
        ok_li, li_data = await client.get_leverage_info(inst_id, td_mode)
        if not ok_li:
            print(
                f"[live_follow] open get_leverage_info fail sim_id={intent.sim_record_id}: "
                f"{li_data!r}"
            )
            _set_live_open_ok(intent.sim_record_id, False)
            return
        picked = sizing_lever_from_leverage_info(
            li_data, hedge_mode=hedge_mode, pos_side=pos_side
        )
        if picked is None:
            print(
                f"[live_follow] open no lever sim_id={intent.sim_record_id} inst={inst_id}"
            )
            _set_live_open_ok(intent.sim_record_id, False)
            return
        sizing_lever = picked

    ok_order, payload = await client.place_swap_market_by_principal_usdt(
        inst_id,
        intent.principal_usdt.strip(),
        leverage=sizing_lever,
        td_mode=td_mode,
        side=side,
        pos_side=pos_side if hedge_mode else None,
    )
    if not ok_order:
        print(
            f"[live_follow] open place_order fail sim_id={intent.sim_record_id} "
            f"pos_id={intent.pos_id!r}: {payload!r}"
        )
        _set_live_open_ok(intent.sim_record_id, False)
        return
    print(
        f"[live_follow] open ok follow_id={intent.follow_account_id} pos_id={intent.pos_id!r} "
        f"inst={inst_id} side={pos_side}"
    )
    _set_live_open_ok(intent.sim_record_id, True)


async def execute_live_follow_close(intent: LiveFollowCloseIntent) -> None:
    db = SessionLocal()
    try:
        cred = db.get(OkxApiAccount, intent.okx_api_account_id)
        if cred is None:
            print(
                f"[live_follow] close skip no cred sim_id={intent.sim_record_id} "
                f"inst={intent.inst_id}"
            )
            _set_live_close_ok(intent.sim_record_id, False)
            return
        client = okx_client_for_db_secrets(cred.api_key, cred.api_secret, cred.api_passphrase)
        if not client.is_configured():
            _set_live_close_ok(intent.sim_record_id, False)
            return
    finally:
        db.close()

    ok_cfg, cfg_data = await client.get_account_config()
    acct_lv, cfg_pos_mode = (
        parse_account_config_fields(cfg_data) if ok_cfg else (None, None)
    )
    blocked = isolated_td_mode_blocked_reason(acct_lv)
    if blocked:
        print(f"[live_follow] close skip sim_id={intent.sim_record_id}: {blocked}")
        _set_live_close_ok(intent.sim_record_id, False)
        return
    td_mode = LIVE_FOLLOW_TD_MODE
    hedge_mode = cfg_pos_mode != "net_mode"

    api_pos_side: str | None
    if hedge_mode:
        ps = (intent.pos_side or "long").lower()
        api_pos_side = ps if ps in ("long", "short") else "long"
    else:
        api_pos_side = None

    ok_cp, data = await client.close_swap_position(
        intent.inst_id, td_mode, api_pos_side
    )
    if not ok_cp:
        print(
            f"[live_follow] close fail follow_id={intent.follow_account_id} "
            f"sim_id={intent.sim_record_id} inst={intent.inst_id}: {data!r}"
        )
        _set_live_close_ok(intent.sim_record_id, False)
        return
    print(
        f"[live_follow] close ok follow_id={intent.follow_account_id} "
        f"sim_id={intent.sim_record_id} inst={intent.inst_id}"
    )
    _set_live_close_ok(intent.sim_record_id, True)


async def run_live_follow_intents(
    close_intents: list[LiveFollowCloseIntent],
    open_intents: list[LiveFollowOpenIntent],
) -> None:
    """先平后开，与同轮快照中欧易侧先关后开的节奏一致。"""
    for it in close_intents:
        await execute_live_follow_close(it)
    for it in open_intents:
        await execute_live_follow_open(it)
