"""
每个启用「自动追加」的跟单帐户独立协程：约每 1 秒拉取该帐户绑定 OKX 的永续持仓（mgnRatio/uplRatio）。
按配置执行自动动作：
1) mgnRatio ≤ 维持保证金率阈值：自动追加逐仓保证金
2) mgnRatio ≤ 平仓保证金率阈值：自动平仓
3) uplRatio ≥ 止盈收益率阈值：自动平仓
4) uplRatio ≤ -止损收益率阈值：自动平仓

默认仅在未配置时回退到 mgnRatio ≤ 2（即 ≤200%，接口为比例）追加：

    追加 USDT = bet_amount_per_position × margin_add_ratio_of_bet

与 position_monitor 相同：主管协程定期对齐 DB 中的目标列表，多帐户并发、互不争抢同一轮询周期。
同一 OKX 密钥 + 同一合约 + 同一 posSide 在进程内用 asyncio.Lock 串行追加；两次成功追加最短间隔见 COOLDOWN_SEC（与轮询周期独立，由该常量单独控制）。
条件：跟单启用、绑定 OKX、真实交易、启动追加、下注金额 > 0、密钥完整。可选 margin_add_max_times
与计数清零规则见下方全局变量说明。

.env 可选 OKX_FOLLOW_REST_BASE、OKX_FOLLOW_USE_PAPER。
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.constant import config as db_config
from config.db import SessionLocal
from module.follow_order import (
    OkxFollowOrderClient,
    add_position_margin,
    get_positions_inst,
    okx_client_for_db_secrets,
)
from v1.Models.follow_account import FollowAccount
from v1.Models.follow_sim_record import FollowSimRecord
from v1.Models.okx_api_account import OkxApiAccount
from v1.Services.okx_contract_helpers import parse_account_config_fields

_last_add_ts: dict[tuple[int, str, str], float] = {}
"""冷却键：(okx_api_accounts.id, instId, posSide)。"""
_last_close_ts: dict[tuple[int, str, str], float] = {}
"""平仓冷却键：(okx_api_accounts.id, instId, posSide|net)。"""
_margin_add_counts: dict[tuple[int, str, str], int] = {}
"""计数键：(follow_accounts.id, instId, posSide)，低于阈值期间累计；mgnRatio（比例）> 阈值后清零。"""
_margin_key_locks: dict[tuple[int, str, str], asyncio.Lock] = {}
"""与冷却键一致：同一 OKX 帐户同一仓位串行化「判冷却 + 调追加接口」，避免多跟单任务并发双发。"""
COOLDOWN_SEC = 0.5
"""同一 OKX 密钥、同一合约、同一 posSide 两次成功追加之间的最短间隔（秒）。"""
CLOSE_COOLDOWN_SEC = 0.5
"""同一 OKX 密钥、同一合约、同一 posSide 两次成功平仓触发之间的最短间隔（秒）。"""
_ACCOUNT_MARGIN_INTERVAL_SEC = 0.5
"""每个跟单帐户协程的轮询间隔（秒）。"""
_SUPERVISOR_INTERVAL_SEC = 0.5
"""主管协程对齐「应监控的跟单 id 列表」的间隔（秒）。"""
# 欧易 GET /account/positions 的 mgnRatio 为比例：2.0 = 200%，与前端 formatMaintMarginRatioPct 的 ×100 一致
MAINT_MARGIN_RATIO_THRESHOLD = 2.0


def _get_margin_lock(key: tuple[int, str, str]) -> asyncio.Lock:
    lock = _margin_key_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _margin_key_locks[key] = lock
    return lock


def _parse_mgn_ratio_api(raw: object) -> float | None:
    """解析持仓 mgnRatio（比例）：去 %、千分位；无法解析返回 None。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("%", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_ratio_value(raw: object) -> float | None:
    """解析比例值（如 uplRatio / 配置阈值）：支持字符串数字、百分号与千分位。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("%", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _effective_mgn_ratio_for_monitor(p: dict[str, Any]) -> float | None:
    """仅使用接口 mgnRatio（比例，2=200%）；空则无法判断。"""
    return _parse_mgn_ratio_api(p.get("mgnRatio"))


def _rows_live_margin_okx(db: Session) -> list[tuple[FollowAccount, OkxApiAccount]]:
    accs = (
        db.execute(
            select(FollowAccount).where(
                FollowAccount.margin_auto_enabled == True,  # noqa: E712
                FollowAccount.enabled == True,  # noqa: E712
                FollowAccount.live_trading_enabled == True,  # noqa: E712
                FollowAccount.okx_api_account_id.isnot(None),
            )
        )
        .scalars()
        .all()
    )
    out: list[tuple[FollowAccount, OkxApiAccount]] = []
    for a in accs:
        if a.bet_amount_per_position is None or a.bet_amount_per_position <= 0:
            continue
        cid = a.okx_api_account_id
        if cid is None:
            continue
        cred = db.get(OkxApiAccount, cid)
        if cred is None:
            continue
        client = okx_client_for_db_secrets(cred.api_key, cred.api_secret, cred.api_passphrase)
        if not client.is_configured():
            continue
        out.append((a, cred))
    return out


def _sync_fetch_margin_follow_ids() -> list[int]:
    """应运行保证金监控的 follow_accounts.id 列表（与 _rows_live_margin_okx 条件一致）。"""
    db = SessionLocal()
    try:
        return [a.id for a, _ in _rows_live_margin_okx(db)]
    finally:
        db.close()


def _sync_load_margin_poll_context(follow_account_id: int) -> dict[str, Any] | None:
    """
    在线程中读取 DB；若当前不应监控则返回 None。
    返回字段供异步轮询使用（含密钥，勿日志打印）。
    """
    db = SessionLocal()
    try:
        acc = db.get(FollowAccount, follow_account_id)
        if acc is None:
            return None
        if not acc.enabled or not acc.margin_auto_enabled or not acc.live_trading_enabled:
            return None
        if acc.okx_api_account_id is None:
            return None
        if acc.bet_amount_per_position is None or acc.bet_amount_per_position <= 0:
            return None
        cred = db.get(OkxApiAccount, acc.okx_api_account_id)
        if cred is None:
            return None
        return {
            "acc_id": acc.id,
            "okx_cred_id": cred.id,
            "bet": acc.bet_amount_per_position,
            "add_ratio": (
                acc.margin_add_ratio_of_bet
                if acc.margin_add_ratio_of_bet is not None
                else Decimal("0.2")
            ),
            "max_times": acc.margin_add_max_times,
            "maint_margin_ratio_threshold": acc.maint_margin_ratio_threshold,
            "close_margin_ratio_threshold": acc.close_margin_ratio_threshold,
            "take_profit_ratio": acc.take_profit_ratio,
            "stop_loss_ratio": acc.stop_loss_ratio,
            "api_key": cred.api_key,
            "api_secret": cred.api_secret,
            "api_passphrase": cred.api_passphrase,
        }
    finally:
        db.close()


def _inst_base_ccy(inst_id: str) -> str:
    s = str(inst_id).strip().upper()
    if "-" not in s:
        return s
    return s.split("-")[0]


def _sync_bump_add_margin_count(acc_id: int, inst_id: str, pos_side: str, amt: str) -> None:
    """将追加保证金次数记到当前 open 的模拟行（同币种同方向最新一条）。"""
    db = SessionLocal()
    try:
        ccy = _inst_base_ccy(inst_id)
        rec = (
            db.execute(
                select(FollowSimRecord)
                .where(
                    FollowSimRecord.follow_account_id == acc_id,
                    FollowSimRecord.status == "open",
                    FollowSimRecord.pos_ccy == ccy,
                    FollowSimRecord.pos_side == pos_side,
                )
                .order_by(FollowSimRecord.id.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
        if rec is None:
            return
        rec.add_margin_count = int(rec.add_margin_count or 0) + 1
        try:
            add_amt = Decimal(str(amt).strip())
        except Exception:
            add_amt = Decimal(0)
        rec.total_invested_usdt = Decimal(str(rec.total_invested_usdt or 0)) + max(add_amt, Decimal(0))
        db.commit()
    finally:
        db.close()


async def _poll_positions_and_maybe_add_margin(
    *,
    acc_id: int,
    okx_cred_id: int,
    bet: Decimal,
    add_ratio: Decimal,
    max_times: int | None,
    maint_margin_ratio_threshold: Decimal | None,
    close_margin_ratio_threshold: Decimal | None,
    take_profit_ratio: Decimal | None,
    stop_loss_ratio: Decimal | None,
    client: OkxFollowOrderClient,
) -> None:
    add_thr_f = (
        _parse_ratio_value(maint_margin_ratio_threshold)
        if maint_margin_ratio_threshold is not None
        else MAINT_MARGIN_RATIO_THRESHOLD
    )
    close_mgn_thr_f = _parse_ratio_value(close_margin_ratio_threshold)
    tp_ratio_f = _parse_ratio_value(take_profit_ratio)
    sl_ratio_f = _parse_ratio_value(stop_loss_ratio)
    ok, data = await get_positions_inst("SWAP", client=client)
    if not ok:
        print(f"[margin_monitor] get_positions follow_id={acc_id} okx_id={okx_cred_id}: {data!r}")
        return
    ok_cfg, cfg_data = await client.get_account_config()
    _, cfg_pos_mode = (
        parse_account_config_fields(cfg_data) if ok_cfg else (None, None)
    )
    net_account = cfg_pos_mode == "net_mode"

    pos_list = data.get("data") or []
    for p in pos_list:
        if not isinstance(p, dict):
            continue
        if (p.get("mgnMode") or "").lower() != "isolated":
            continue
        inst_id = str(p.get("instId") or "").strip()
        if not inst_id:
            continue
        pos_side_raw = (p.get("posSide") or "net").lower()
        if pos_side_raw not in ("long", "short", "net"):
            pos_side_raw = "net"
        api_pos_side = "net" if net_account else pos_side_raw
        try:
            pos_sz = float(str(p.get("pos") or "").strip() or "0")
        except (TypeError, ValueError):
            pos_sz = 0.0
        if abs(pos_sz) < 1e-12:
            continue
        mgn = _effective_mgn_ratio_for_monitor(p)
        if mgn is None:
            mr = p.get("mgnRatio")
            if mr is not None and str(mr).strip() != "":
                print(
                    f"[margin_monitor] skip unparseable mgnRatio follow_id={acc_id} "
                    f"{inst_id!r} raw={mr!r}"
                )
            continue
        upl_ratio = _parse_ratio_value(p.get("uplRatio"))

        # 风控平仓优先于追加：低平仓保证金率 / 达止盈 / 达止损
        close_reasons: list[str] = []
        if close_mgn_thr_f is not None and mgn <= close_mgn_thr_f:
            close_reasons.append(f"mgnRatio<={close_mgn_thr_f}")
        if tp_ratio_f is not None and upl_ratio is not None and upl_ratio >= tp_ratio_f:
            close_reasons.append(f"uplRatio>={tp_ratio_f}")
        if sl_ratio_f is not None and upl_ratio is not None and upl_ratio <= -sl_ratio_f:
            close_reasons.append(f"uplRatio<=-{sl_ratio_f}")
        if close_reasons:
            close_side = None if net_account else (pos_side_raw if pos_side_raw in ("long", "short") else None)
            close_key = (okx_cred_id, inst_id.upper(), close_side or "net")
            close_lock = _get_margin_lock(close_key)
            async with close_lock:
                now = time.time()
                if now - _last_close_ts.get(close_key, 0) < CLOSE_COOLDOWN_SEC:
                    continue
                ok_close, close_res = await client.close_swap_position(
                    inst_id, "isolated", close_side
                )
                if ok_close:
                    _last_close_ts[close_key] = time.time()
                    _margin_add_counts.pop((acc_id, inst_id.lower(), api_pos_side), None)
                    print(
                        f"[margin_monitor] close ok follow_id={acc_id} okx_id={okx_cred_id} "
                        f"{inst_id} posSide={close_side or 'net'} reason={'+'.join(close_reasons)} "
                        f"mgnRatio~={mgn} uplRatio~={upl_ratio}"
                    )
                else:
                    print(
                        f"[margin_monitor] close fail follow_id={acc_id} {inst_id} "
                        f"posSide={close_side or 'net'} reason={'+'.join(close_reasons)}: {close_res!r}"
                    )
            await asyncio.sleep(0.35)
            continue

        count_key = (acc_id, inst_id.lower(), api_pos_side)
        # 比例 > 阈值视为安全并清零计数；≤ 阈值（含等于）则尝试追加
        if mgn > add_thr_f:
            _margin_add_counts.pop(count_key, None)
            continue
        if max_times is not None and _margin_add_counts.get(count_key, 0) >= max_times:
            continue
        cooldown_key = (okx_cred_id, inst_id.upper(), api_pos_side)
        add_amt: Decimal = bet * add_ratio  # type: ignore[operator]
        amt_str = f"{float(add_amt):.8f}".rstrip("0").rstrip(".")
        if not amt_str or (
            amt_str.replace(".", "", 1).isdigit() and float(amt_str) <= 0
        ):
            continue
        lock = _get_margin_lock(cooldown_key)
        async with lock:
            now = time.time()
            if now - _last_add_ts.get(cooldown_key, 0) < COOLDOWN_SEC:
                continue
            ok2, res = await add_position_margin(
                inst_id, api_pos_side, amt_str, client=client
            )
            if ok2:
                _last_add_ts[cooldown_key] = time.time()
                _margin_add_counts[count_key] = _margin_add_counts.get(count_key, 0) + 1
                await asyncio.to_thread(_sync_bump_add_margin_count, acc_id, inst_id, pos_side_raw, amt_str)
                print(
                    f"[margin_monitor] add margin ok follow_id={acc_id} okx_id={okx_cred_id} "
                    f"{inst_id} {api_pos_side} amt={amt_str} mgnRatio~={mgn} "
                    f"count={_margin_add_counts[count_key]} cooldown>={COOLDOWN_SEC}s"
                )
            else:
                print(
                    f"[margin_monitor] add margin fail follow_id={acc_id} {inst_id} "
                    f"posSide={api_pos_side}: {res!r}"
                )
        await asyncio.sleep(0.35)


async def _account_margin_loop(follow_account_id: int) -> None:
    """单条跟单记录：约每秒读 DB 配置并拉取本人 OKX 持仓，与其它跟单帐户并发。"""
    while True:
        try:
            ctx = await asyncio.to_thread(_sync_load_margin_poll_context, follow_account_id)
            if ctx is None:
                await asyncio.sleep(_ACCOUNT_MARGIN_INTERVAL_SEC)
                continue
            client = okx_client_for_db_secrets(
                str(ctx["api_key"]),
                str(ctx["api_secret"]),
                str(ctx["api_passphrase"]),
            )
            if not client.is_configured():
                await asyncio.sleep(_ACCOUNT_MARGIN_INTERVAL_SEC)
                continue
            await _poll_positions_and_maybe_add_margin(
                acc_id=int(ctx["acc_id"]),
                okx_cred_id=int(ctx["okx_cred_id"]),
                bet=ctx["bet"],
                add_ratio=ctx["add_ratio"],
                max_times=ctx["max_times"],
                maint_margin_ratio_threshold=ctx["maint_margin_ratio_threshold"],
                close_margin_ratio_threshold=ctx["close_margin_ratio_threshold"],
                take_profit_ratio=ctx["take_profit_ratio"],
                stop_loss_ratio=ctx["stop_loss_ratio"],
                client=client,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[margin_monitor] follow_id={follow_account_id}: {e!r}")
        await asyncio.sleep(_ACCOUNT_MARGIN_INTERVAL_SEC)


async def margin_monitor_loop() -> None:
    """
    对每个满足条件的跟单帐户单独 `asyncio.create_task`，各任务约 1s 一轮询；
    主管协程约每 1s 对齐 DB 列表（启用/关闭追加、解绑 OKX 等会增删任务）。
    """
    tasks: dict[int, asyncio.Task] = {}
    while True:
        try:
            if not db_config.MYSQL_DB:
                for t in tasks.values():
                    t.cancel()
                if tasks:
                    await asyncio.gather(*tasks.values(), return_exceptions=True)
                tasks.clear()
                await asyncio.sleep(5)
                continue

            want_ids = set(await asyncio.to_thread(_sync_fetch_margin_follow_ids))

            for aid, t in list(tasks.items()):
                if aid not in want_ids:
                    t.cancel()

            for aid, t in list(tasks.items()):
                if aid not in want_ids:
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                    tasks.pop(aid, None)

            for aid in want_ids:
                if aid not in tasks:
                    tasks[aid] = asyncio.create_task(_account_margin_loop(aid))

            await asyncio.sleep(_SUPERVISOR_INTERVAL_SEC)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[margin_monitor] supervisor: {e!r}")
            await asyncio.sleep(2)
