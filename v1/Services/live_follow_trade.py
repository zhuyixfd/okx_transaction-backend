"""
自动跟单真实开平仓：与 POST /manual-okx/contract-order 相同的账户配置、设杠杆与按保证金市价开仓；
对方平仓且本笔曾在 OKX 开仓成功时，调用 close-position 市价平仓。

开仓防重：sim 已标记 live_open_ok 则跳过；同一 OKX 帐户 + 合约 + 方向 asyncio.Lock 串行；
下单前拉取 SWAP 逐仓持仓，若已有同向持仓则不再下单（避免快照抖动或并发导致重复买）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

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

_live_open_locks: dict[tuple[int, str, str], asyncio.Lock] = {}
"""(okx_api_accounts.id, instId 大写, pos_side long|short) 下同进程串行开仓。"""


def _get_live_open_lock(key: tuple[int, str, str]) -> asyncio.Lock:
    lock = _live_open_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _live_open_locks[key] = lock
    return lock


def _pos_sz_float(raw: object) -> float:
    try:
        return float(str(raw).strip() or "0")
    except (TypeError, ValueError):
        return 0.0


def _okx_swap_row_matches_follow_open(
    p: dict,
    *,
    inst_id: str,
    want_side: str,
    hedge_mode: bool,
) -> bool:
    """是否为本策略要开的逐仓永续持仓（有张数）。"""
    if (p.get("instId") or "").strip().upper() != inst_id.strip().upper():
        return False
    if (p.get("mgnMode") or "").lower() != "isolated":
        return False
    sz = _pos_sz_float(p.get("pos"))
    if abs(sz) < 1e-12:
        return False
    ws = want_side.strip().lower()
    ps = (p.get("posSide") or "net").strip().lower()
    if hedge_mode:
        if ps not in ("long", "short"):
            return False
        return ps == ws
    if ws == "long":
        return sz > 0
    if ws == "short":
        return sz < 0
    return False


@dataclass(frozen=True)
class LiveFollowOpenIntent:
    follow_account_id: int
    okx_api_account_id: int
    sim_record_id: int
    pos_id: str
    inst_id: str
    pos_side: str
    lever_str: str | None
    contracts: str


@dataclass(frozen=True)
class LiveFollowCloseIntent:
    follow_account_id: int
    okx_api_account_id: int
    sim_record_id: int
    inst_id: str
    pos_side: str | None


@dataclass(frozen=True)
class LiveFollowAdjustIntent:
    follow_account_id: int
    okx_api_account_id: int
    sim_record_id: int
    inst_id: str
    pos_side: str
    lever_str: str | None
    action: str  # add | reduce | rebalance
    contracts: str  # add/reduce 为变动张数；rebalance 为目标总张数


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


def _inst_base_ccy(inst_id: str) -> str:
    s = str(inst_id or "").strip().upper()
    if not s:
        return ""
    i = s.find("-")
    return s[:i] if i > 0 else s


def _is_ccy_manually_blocked(follow_account_id: int, inst_id: str) -> bool:
    """该币种最新记录若为 closed 且 live_close_ok=True，则视为手动关闭该币跟单。"""
    ccy = _inst_base_ccy(inst_id)
    if not ccy:
        return False
    db = SessionLocal()
    try:
        rec = (
            db.execute(
                select(FollowSimRecord)
                .where(
                    FollowSimRecord.follow_account_id == follow_account_id,
                    FollowSimRecord.pos_ccy == ccy,
                )
                .order_by(FollowSimRecord.id.desc())
                .limit(1)
            )
            .scalar_one_or_none()
        )
        return bool(rec is not None and rec.status == "closed" and rec.live_close_ok is True)
    finally:
        db.close()


async def execute_live_follow_open(intent: LiveFollowOpenIntent) -> None:
    db = SessionLocal()
    try:
        sim = db.get(FollowSimRecord, intent.sim_record_id)
        if sim is None:
            return
        if sim.live_open_ok is True:
            return
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
    lock_key = (
        intent.okx_api_account_id,
        inst_id.strip().upper(),
        pos_side.strip().lower(),
    )
    lock = _get_live_open_lock(lock_key)
    async with lock:
        if _is_ccy_manually_blocked(intent.follow_account_id, intent.inst_id):
            print(
                f"[live_follow] open skip manually_blocked_ccy sim_id={intent.sim_record_id} "
                f"inst={intent.inst_id}"
            )
            _set_live_open_ok(intent.sim_record_id, False)
            return

        db_chk = SessionLocal()
        try:
            sim2 = db_chk.get(FollowSimRecord, intent.sim_record_id)
            if sim2 is not None and sim2.live_open_ok is True:
                return
        finally:
            db_chk.close()

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

        ok_pos, pos_data = await client.get_positions_inst("SWAP")
        if ok_pos and isinstance(pos_data, dict):
            for row in pos_data.get("data") or []:
                if not isinstance(row, dict):
                    continue
                if _okx_swap_row_matches_follow_open(
                    row,
                    inst_id=inst_id,
                    want_side=pos_side,
                    hedge_mode=hedge_mode,
                ):
                    print(
                        f"[live_follow] open skip already has position sim_id={intent.sim_record_id} "
                        f"pos_id={intent.pos_id!r} inst={inst_id} side={pos_side}"
                    )
                    _set_live_open_ok(intent.sim_record_id, True)
                    return

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

        contracts = intent.contracts.strip()
        ok_order, payload = await client.place_swap_market_by_sz(
            inst_id,
            contracts,
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


def _dec(raw: object) -> Decimal | None:
    try:
        d = Decimal(str(raw).strip())
    except Exception:
        return None
    return d


async def execute_live_follow_adjust(intent: LiveFollowAdjustIntent) -> None:
    db = SessionLocal()
    try:
        cred = db.get(OkxApiAccount, intent.okx_api_account_id)
        if cred is None:
            return
        client = okx_client_for_db_secrets(cred.api_key, cred.api_secret, cred.api_passphrase)
        if not client.is_configured():
            return
    finally:
        db.close()

    ok_cfg, cfg_data = await client.get_account_config()
    acct_lv, cfg_pos_mode = (
        parse_account_config_fields(cfg_data) if ok_cfg else (None, None)
    )
    blocked = isolated_td_mode_blocked_reason(acct_lv)
    if blocked:
        return
    td_mode = LIVE_FOLLOW_TD_MODE
    hedge_mode = cfg_pos_mode != "net_mode"

    ok_pos, pos_data = await client.get_positions_inst("SWAP")
    if not ok_pos or not isinstance(pos_data, dict):
        return
    target_row: dict | None = None
    for row in pos_data.get("data") or []:
        if not isinstance(row, dict):
            continue
        if not _okx_swap_row_matches_follow_open(
            row,
            inst_id=intent.inst_id,
            want_side=intent.pos_side,
            hedge_mode=hedge_mode,
        ):
            continue
        target_row = row
        break
    contracts = intent.contracts.strip()
    if not contracts:
        return

    side: str
    place_contracts = contracts
    if intent.action == "rebalance":
        want = _dec(contracts)
        if want is None or want <= 0:
            return
        cur = Decimal("0")
        if target_row is not None:
            c = _dec(target_row.get("pos"))
            if c is not None:
                cur = abs(c)
        diff = want - cur
        if abs(diff) < Decimal("1e-12"):
            return
        place_contracts = format(abs(diff), "f").rstrip("0").rstrip(".")
        if not place_contracts:
            return
        if diff > 0:
            side = "buy" if intent.pos_side == "long" else "sell"
        else:
            side = "sell" if intent.pos_side == "long" else "buy"
    elif intent.action == "add":
        side = "buy" if intent.pos_side == "long" else "sell"
    else:
        side = "sell" if intent.pos_side == "long" else "buy"
    ok_order, _ = await client.place_swap_market_by_sz(
        intent.inst_id,
        place_contracts,
        td_mode=td_mode,
        side=side,
        pos_side=intent.pos_side if hedge_mode else None,
    )
    if not ok_order:
        return


async def run_live_follow_intents(
    close_intents: list[LiveFollowCloseIntent],
    open_intents: list[LiveFollowOpenIntent],
    adjust_intents: list[LiveFollowAdjustIntent] | None = None,
) -> None:
    """先平后开，与同轮快照中欧易侧先关后开的节奏一致。"""
    for it in close_intents:
        await execute_live_follow_close(it)
    # 同一轮内同一交易员 posId 只执行一次开仓（防御重复 intent / 多进程竞态）
    seen_open: set[tuple[int, str]] = set()
    for it in open_intents:
        key = (it.follow_account_id, it.pos_id)
        if key in seen_open:
            continue
        seen_open.add(key)
        await execute_live_follow_open(it)
    for it in (adjust_intents or []):
        await execute_live_follow_adjust(it)
