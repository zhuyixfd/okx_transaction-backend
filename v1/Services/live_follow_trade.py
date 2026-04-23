"""
自动跟单真实开平仓：与 POST /manual-okx/contract-order 相同的账户配置、设杠杆与按保证金市价开仓；
对方平仓且本笔曾在 OKX 开仓成功时，调用 close-position 市价平仓。

开仓防重：sim 已标记 live_open_ok 则跳过；同一 OKX 帐户 + 合约 + 方向 asyncio.Lock 串行；
下单前拉取 SWAP 逐仓持仓，若已有同向持仓则不再下单（避免快照抖动或并发导致重复买）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
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
REBALANCE_DEADBAND_CONTRACTS = Decimal("0.01")
TRADE_ACTION_DEBOUNCE_SEC = 3.0
MIN_SZ_FAIL_COOLDOWN_SEC = 10.0

_live_open_locks: dict[tuple[int, str, str], asyncio.Lock] = {}
"""(okx_api_accounts.id, instId 大写, pos_side long|short) 下同进程串行开仓。"""
_live_trade_success_last_ts: dict[tuple[int, str, str], float] = {}
_live_min_sz_fail_last_ts: dict[tuple[int, str, str], float] = {}


def _summarize_order_error(payload: object) -> str:
    if not isinstance(payload, dict):
        s = str(payload)
        return s[:1000]
    code = str(payload.get("code", "")).strip()
    msg = str(payload.get("msg", "")).strip()
    data = payload.get("data")
    detail = ""
    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            sc = str(first.get("sCode", "")).strip()
            sm = str(first.get("sMsg", "")).strip()
            if sc or sm:
                detail = f"sCode={sc} sMsg={sm}".strip()
    parts = [p for p in (f"code={code}" if code else "", f"msg={msg}" if msg else "", detail) if p]
    out = " | ".join(parts) if parts else str(payload)
    return out[:1000]


def _trade_action_cooldown_hit(okx_api_account_id: int, inst_id: str, pos_side: str | None) -> bool:
    """
    同币种同方向 3 秒内只允许一次“加/减/平”成功动作。
    返回 True 表示仍在冷却中，应跳过本次执行。
    """
    side = (pos_side or "net").strip().lower()
    key = (okx_api_account_id, inst_id.strip().upper(), side)
    now_ts = time.monotonic()
    last_ts = _live_trade_success_last_ts.get(key, 0.0)
    if (now_ts - last_ts) < TRADE_ACTION_DEBOUNCE_SEC:
        return True
    return False


def _mark_trade_action_success(
    okx_api_account_id: int, inst_id: str, pos_side: str | None
) -> None:
    side = (pos_side or "net").strip().lower()
    key = (okx_api_account_id, inst_id.strip().upper(), side)
    _live_trade_success_last_ts[key] = time.monotonic()


def _mark_min_sz_fail(okx_api_account_id: int, inst_id: str, pos_side: str | None) -> None:
    side = (pos_side or "net").strip().lower()
    key = (okx_api_account_id, inst_id.strip().upper(), side)
    _live_min_sz_fail_last_ts[key] = time.monotonic()


def _in_min_sz_fail_cooldown(okx_api_account_id: int, inst_id: str, pos_side: str | None) -> bool:
    side = (pos_side or "net").strip().lower()
    key = (okx_api_account_id, inst_id.strip().upper(), side)
    last_ts = _live_min_sz_fail_last_ts.get(key, 0.0)
    if last_ts <= 0:
        return False
    return (time.monotonic() - last_ts) < MIN_SZ_FAIL_COOLDOWN_SEC


def _trade_guard_lock_key(okx_api_account_id: int, inst_id: str, pos_side: str | None) -> str:
    side = (pos_side or "net").strip().lower() or "net"
    inst = inst_id.strip().upper()
    return f"live_follow:{okx_api_account_id}:{inst}:{side}"


def _acquire_trade_guard_lock(
    okx_api_account_id: int,
    inst_id: str,
    pos_side: str | None,
    timeout_sec: int = 0,
) -> SessionLocal | None:
    """
    跨进程互斥：基于 MySQL GET_LOCK，避免多进程重复对同一标的同向下单。
    成功返回持锁 Session；失败返回 None。
    """
    db = SessionLocal()
    try:
        key = _trade_guard_lock_key(okx_api_account_id, inst_id, pos_side)
        got = db.execute(
            text("SELECT GET_LOCK(:k, :t)"),
            {"k": key, "t": int(timeout_sec)},
        ).scalar_one_or_none()
        if int(got or 0) != 1:
            db.close()
            return None
        return db
    except Exception:
        db.close()
        return None


async def _acquire_trade_guard_lock_with_retry(
    okx_api_account_id: int,
    inst_id: str,
    pos_side: str | None,
    *,
    timeout_sec: int = 1,
    retry_delay_sec: float = 0.2,
) -> SessionLocal | None:
    """
    进程锁获取（异步轮询）：
    - 每次用 GET_LOCK(..., 0) 非阻塞尝试，避免阻塞事件循环
    - 在 timeout_sec 时间窗口内按 retry_delay_sec 重试
    """
    deadline = time.monotonic() + max(float(timeout_sec), 0.0)
    while True:
        db = _acquire_trade_guard_lock(okx_api_account_id, inst_id, pos_side, timeout_sec=0)
        if db is not None:
            return db
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(max(retry_delay_sec, 0.05))


def _release_trade_guard_lock(
    db: SessionLocal | None,
    okx_api_account_id: int,
    inst_id: str,
    pos_side: str | None,
) -> None:
    if db is None:
        return
    try:
        key = _trade_guard_lock_key(okx_api_account_id, inst_id, pos_side)
        db.execute(text("SELECT RELEASE_LOCK(:k)"), {"k": key})
    except Exception:
        pass
    finally:
        db.close()


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
    force: bool = False


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


def _set_live_last_error(sim_id: int, value: str | None) -> None:
    db = SessionLocal()
    try:
        r = db.get(FollowSimRecord, sim_id)
        if r:
            r.live_last_error = value[:1000] if value else None
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


def _side_block_pid(ccy: str, side: str) -> str:
    return f"__side_block__:{ccy.strip().upper()}:{side.strip().lower()}"


def _is_ccy_side_manually_blocked(follow_account_id: int, inst_id: str, side: str) -> bool:
    base = (inst_id or "").strip().upper().split("-")[0]
    s = (side or "").strip().lower()
    if not base or s not in ("long", "short"):
        return False
    pid = _side_block_pid(base, s)
    db = SessionLocal()
    try:
        rec = (
            db.execute(
                select(FollowSimRecord)
                .where(
                    FollowSimRecord.follow_account_id == follow_account_id,
                    FollowSimRecord.pos_id == pid,
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
            _set_live_last_error(intent.sim_record_id, "open skip: no okx credential")
            return
        client = okx_client_for_db_secrets(cred.api_key, cred.api_secret, cred.api_passphrase)
        if not client.is_configured():
            print(
                f"[live_follow] open skip unconfigured okx_id={intent.okx_api_account_id} "
                f"sim_id={intent.sim_record_id}"
            )
            _set_live_open_ok(intent.sim_record_id, False)
            _set_live_last_error(intent.sim_record_id, "open skip: unconfigured okx api")
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
        guard_db = await _acquire_trade_guard_lock_with_retry(
            intent.okx_api_account_id, intent.inst_id, intent.pos_side, timeout_sec=1
        )
        if guard_db is None:
            print(
                f"[live_follow] open skip guard_lock_timeout_after_retry sim_id={intent.sim_record_id} "
                f"inst={intent.inst_id} side={intent.pos_side}"
            )
            return
        try:
            if _is_ccy_side_manually_blocked(intent.follow_account_id, intent.inst_id, intent.pos_side):
                print(
                    f"[live_follow] open skip manually_blocked_side sim_id={intent.sim_record_id} "
                    f"inst={intent.inst_id} side={intent.pos_side}"
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
                _set_live_last_error(intent.sim_record_id, f"open skip: {blocked}")
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
                    _set_live_last_error(intent.sim_record_id, _summarize_order_error(pm_data))
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
                    _set_live_last_error(intent.sim_record_id, "open fail: invalid lever")
                    return
                if not (1 <= lv <= 125):
                    print(f"[live_follow] open lever OOB sim_id={intent.sim_record_id}: {lv}")
                    _set_live_open_ok(intent.sim_record_id, False)
                    _set_live_last_error(intent.sim_record_id, "open fail: lever out of range")
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
                    _set_live_last_error(intent.sim_record_id, _summarize_order_error(lev_data))
                    return
            else:
                ok_li, li_data = await client.get_leverage_info(inst_id, td_mode)
                if not ok_li:
                    print(
                        f"[live_follow] open get_leverage_info fail sim_id={intent.sim_record_id}: "
                        f"{li_data!r}"
                    )
                    _set_live_open_ok(intent.sim_record_id, False)
                    _set_live_last_error(intent.sim_record_id, _summarize_order_error(li_data))
                    return
                picked = sizing_lever_from_leverage_info(
                    li_data, hedge_mode=hedge_mode, pos_side=pos_side
                )
                if picked is None:
                    print(
                        f"[live_follow] open no lever sim_id={intent.sim_record_id} inst={inst_id}"
                    )
                    _set_live_open_ok(intent.sim_record_id, False)
                    _set_live_last_error(intent.sim_record_id, "open fail: no leverage available")
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
                _set_live_last_error(intent.sim_record_id, _summarize_order_error(payload))
                return
            print(
                f"[live_follow] open ok follow_id={intent.follow_account_id} pos_id={intent.pos_id!r} "
                f"inst={inst_id} side={pos_side}"
            )
            _set_live_open_ok(intent.sim_record_id, True)
            _set_live_last_error(intent.sim_record_id, None)
        finally:
            _release_trade_guard_lock(
                guard_db, intent.okx_api_account_id, intent.inst_id, intent.pos_side
            )


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
            _set_live_last_error(intent.sim_record_id, "close skip: no okx credential")
            return
        client = okx_client_for_db_secrets(cred.api_key, cred.api_secret, cred.api_passphrase)
        if not client.is_configured():
            _set_live_close_ok(intent.sim_record_id, False)
            _set_live_last_error(intent.sim_record_id, "close skip: unconfigured okx api")
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
        _set_live_last_error(intent.sim_record_id, f"close skip: {blocked}")
        return
    td_mode = LIVE_FOLLOW_TD_MODE
    hedge_mode = cfg_pos_mode != "net_mode"

    api_pos_side: str | None
    if hedge_mode:
        ps = (intent.pos_side or "long").lower()
        api_pos_side = ps if ps in ("long", "short") else "long"
    else:
        api_pos_side = None

    if (not intent.force) and _trade_action_cooldown_hit(
        intent.okx_api_account_id, intent.inst_id, api_pos_side
    ):
        print(
            f"[live_follow] close skip debounce follow_id={intent.follow_account_id} "
            f"sim_id={intent.sim_record_id} inst={intent.inst_id}"
        )
        return
    guard_db = await _acquire_trade_guard_lock_with_retry(
        intent.okx_api_account_id,
        intent.inst_id,
        api_pos_side,
        timeout_sec=2 if intent.force else 1,
    )
    if guard_db is None:
        print(
            f"[live_follow] close skip guard_lock_timeout_after_retry follow_id={intent.follow_account_id} "
            f"sim_id={intent.sim_record_id} inst={intent.inst_id}"
        )
        return

    try:
        ok_cp, data = await client.close_swap_position(
            intent.inst_id, td_mode, api_pos_side
        )
        if not ok_cp:
            print(
                f"[live_follow] close fail follow_id={intent.follow_account_id} "
                f"sim_id={intent.sim_record_id} inst={intent.inst_id}: {data!r}"
            )
            _set_live_close_ok(intent.sim_record_id, False)
            _set_live_last_error(intent.sim_record_id, _summarize_order_error(data))
            return
        print(
            f"[live_follow] close ok follow_id={intent.follow_account_id} "
            f"sim_id={intent.sim_record_id} inst={intent.inst_id}"
        )
        _mark_trade_action_success(intent.okx_api_account_id, intent.inst_id, api_pos_side)
        _set_live_close_ok(intent.sim_record_id, True)
        _set_live_last_error(intent.sim_record_id, None)
    finally:
        _release_trade_guard_lock(guard_db, intent.okx_api_account_id, intent.inst_id, api_pos_side)


def _dec(raw: object) -> Decimal | None:
    try:
        d = Decimal(str(raw).strip())
    except Exception:
        return None
    return d


async def execute_live_follow_adjust(intent: LiveFollowAdjustIntent) -> None:
    if _is_ccy_side_manually_blocked(intent.follow_account_id, intent.inst_id, intent.pos_side):
        print(
            f"[live_follow] adjust skip paused_side follow_id={intent.follow_account_id} "
            f"sim_id={intent.sim_record_id} inst={intent.inst_id} side={intent.pos_side}"
        )
        return
    if _trade_action_cooldown_hit(intent.okx_api_account_id, intent.inst_id, intent.pos_side):
        print(
            f"[live_follow] adjust skip debounce follow_id={intent.follow_account_id} "
            f"sim_id={intent.sim_record_id} inst={intent.inst_id} side={intent.pos_side}"
        )
        return
    if _in_min_sz_fail_cooldown(intent.okx_api_account_id, intent.inst_id, intent.pos_side):
        print(
            f"[live_follow] adjust skip min_sz_cooldown follow_id={intent.follow_account_id} "
            f"sim_id={intent.sim_record_id} inst={intent.inst_id} side={intent.pos_side}"
        )
        return
    guard_db = await _acquire_trade_guard_lock_with_retry(
        intent.okx_api_account_id, intent.inst_id, intent.pos_side, timeout_sec=1
    )
    if guard_db is None:
        print(
            f"[live_follow] adjust skip guard_lock_timeout_after_retry follow_id={intent.follow_account_id} "
            f"sim_id={intent.sim_record_id} inst={intent.inst_id} side={intent.pos_side}"
        )
        return
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
        print(
            f"[live_follow] adjust skip get_positions_fail follow_id={intent.follow_account_id} "
            f"sim_id={intent.sim_record_id} inst={intent.inst_id} side={intent.pos_side}"
        )
        return
    cur = Decimal("0")
    for row in pos_data.get("data") or []:
        if not isinstance(row, dict):
            continue
        if (row.get("instId") or "").strip().upper() != intent.inst_id.strip().upper():
            continue
        c = _dec(row.get("pos"))
        if c is None or abs(c) < Decimal("1e-12"):
            continue
        if hedge_mode:
            row_side = (row.get("posSide") or "").strip().lower()
            if row_side != intent.pos_side:
                continue
            cur += abs(c)
        else:
            if intent.pos_side == "long" and c > 0:
                cur += abs(c)
            elif intent.pos_side == "short" and c < 0:
                cur += abs(c)
    contracts = intent.contracts.strip()
    if not contracts:
        print(
            f"[live_follow] adjust skip empty_contracts follow_id={intent.follow_account_id} "
            f"sim_id={intent.sim_record_id} inst={intent.inst_id} side={intent.pos_side}"
        )
        return

    side: str
    place_contracts = contracts
    if intent.action == "rebalance":
        want = _dec(contracts)
        if want is None or want <= 0:
            print(
                f"[live_follow] adjust skip invalid_target follow_id={intent.follow_account_id} "
                f"sim_id={intent.sim_record_id} inst={intent.inst_id} side={intent.pos_side} "
                f"target={contracts!r}"
            )
            return
        diff = want - cur
        if abs(diff) < REBALANCE_DEADBAND_CONTRACTS:
            print(
                f"[live_follow] adjust skip deadband follow_id={intent.follow_account_id} "
                f"sim_id={intent.sim_record_id} inst={intent.inst_id} side={intent.pos_side} "
                f"want={format(want, 'f')} cur={format(cur, 'f')} diff={format(diff, 'f')}"
            )
            return
        place_contracts = format(abs(diff), "f").rstrip("0").rstrip(".")
        if not place_contracts:
            print(
                f"[live_follow] adjust skip empty_place_contracts follow_id={intent.follow_account_id} "
                f"sim_id={intent.sim_record_id} inst={intent.inst_id} side={intent.pos_side}"
            )
            return
        if diff > 0:
            side = "buy" if intent.pos_side == "long" else "sell"
        else:
            side = "sell" if intent.pos_side == "long" else "buy"
    elif intent.action == "add":
        side = "buy" if intent.pos_side == "long" else "sell"
    else:
        side = "sell" if intent.pos_side == "long" else "buy"
    try:
        ok_order, order_payload = await client.place_swap_market_by_sz(
            intent.inst_id,
            place_contracts,
            td_mode=td_mode,
            side=side,
            pos_side=intent.pos_side if hedge_mode else None,
        )
        if not ok_order:
            print(
                f"[live_follow] adjust fail follow_id={intent.follow_account_id} "
                f"sim_id={intent.sim_record_id} inst={intent.inst_id} side={intent.pos_side} "
                f"action={intent.action} place_side={side} place_contracts={place_contracts} "
                f"payload={order_payload!r}"
            )
            _set_live_last_error(intent.sim_record_id, _summarize_order_error(order_payload))
            payload_msg = str(order_payload.get("msg", "")).strip() if isinstance(order_payload, dict) else ""
            payload_data = order_payload.get("data") if isinstance(order_payload, dict) else None
            s_msg = ""
            if isinstance(payload_data, list) and payload_data and isinstance(payload_data[0], dict):
                s_msg = str(payload_data[0].get("sMsg", "")).strip()
            if ("最小下单量" in payload_msg) or ("minimum order size" in payload_msg.lower()) or ("最小下单量" in s_msg):
                _mark_min_sz_fail(intent.okx_api_account_id, intent.inst_id, intent.pos_side)
            return
        print(
            f"[live_follow] adjust ok follow_id={intent.follow_account_id} "
            f"sim_id={intent.sim_record_id} inst={intent.inst_id} side={intent.pos_side} "
            f"action={intent.action} target={contracts} cur={format(cur, 'f')} place={place_contracts}"
        )
        _mark_trade_action_success(intent.okx_api_account_id, intent.inst_id, intent.pos_side)
        _set_live_last_error(intent.sim_record_id, None)
    finally:
        _release_trade_guard_lock(guard_db, intent.okx_api_account_id, intent.inst_id, intent.pos_side)


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
    # 同一轮内同一交易员+合约+方向的调仓 intent 合并。
    # 对 rebalance：目标张数按同 key 求和，避免同币种同方向多仓时“只取最后一条”导致少跟。
    # 对 add/reduce：仍保留最后一条，避免同轮动作打架。
    merged_adjust: dict[tuple[int, str, str], LiveFollowAdjustIntent] = {}
    for it in (adjust_intents or []):
        key = (
            it.follow_account_id,
            it.inst_id.strip().upper(),
            it.pos_side.strip().lower(),
        )
        prev = merged_adjust.get(key)
        if (
            prev is not None
            and prev.action == "rebalance"
            and it.action == "rebalance"
        ):
            try:
                merged_target = (
                    Decimal(str(prev.contracts).strip()) + Decimal(str(it.contracts).strip())
                )
                merged_adjust[key] = LiveFollowAdjustIntent(
                    follow_account_id=it.follow_account_id,
                    okx_api_account_id=it.okx_api_account_id,
                    sim_record_id=it.sim_record_id,
                    inst_id=it.inst_id,
                    pos_side=it.pos_side,
                    lever_str=it.lever_str or prev.lever_str,
                    action="rebalance",
                    contracts=format(merged_target, "f").rstrip("0").rstrip(".") or "0",
                )
            except Exception:
                merged_adjust[key] = it
            continue
        merged_adjust[key] = it
    # 不同币种/方向调仓并发执行，避免单一币种锁等待/失败拖住其它币种。
    merged_adjust_items = list(merged_adjust.values())
    adjust_tasks = [asyncio.create_task(execute_live_follow_adjust(it)) for it in merged_adjust_items]
    if adjust_tasks:
        results = await asyncio.gather(*adjust_tasks, return_exceptions=True)
        for idx, r in enumerate(results):
            if isinstance(r, Exception):
                it = merged_adjust_items[idx]
                print(
                    f"[live_follow] adjust task error follow_id={it.follow_account_id} "
                    f"inst={it.inst_id} side={it.pos_side}: {r!r}"
                )
