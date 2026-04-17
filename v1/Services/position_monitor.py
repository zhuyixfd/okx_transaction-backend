from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.cn_time import now_cn
from config.constant import config as db_config
from config.db import SessionLocal
from module import OkxTrade
from module.trade import pick_lever_from_pos
from v1.Models.follow_account import FollowAccount
from v1.Models.follow_position import FollowPositionEvent, FollowPositionSnapshot
from v1.Models.follow_sim_record import FollowSimRecord
from v1.Services.live_follow_trade import (
    LiveFollowAdjustIntent,
    LiveFollowCloseIntent,
    LiveFollowOpenIntent,
    run_live_follow_intents,
)
from v1.Services.okx_contract_helpers import normalize_swap_inst_id


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    print(f"[{ts}] [position_monitor] {msg}")


def _c_time_key(p: dict[str, Any]) -> tuple[int, str]:
    ct = p.get("cTime")
    try:
        ct_i = int(ct) if ct is not None and str(ct).strip() != "" else 0
    except (TypeError, ValueError):
        ct_i = 0
    return (ct_i, str(p.get("posId", "")))


def _unique_positions_by_pos_id(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    按 posId 去重：同一 posId 多行时保留 cTime 最小的一行（先开的优先），与快照 new_map 语义一致。
    """
    best: dict[str, dict[str, Any]] = {}
    for p in positions:
        if p.get("posId") is None:
            continue
        pid = str(p["posId"])
        if pid not in best:
            best[pid] = p
        elif _c_time_key(p) < _c_time_key(best[pid]):
            best[pid] = p
    return list(best.values())


def _sim_eligible_from_unique(
    unique: list[dict[str, Any]],
    max_n: int | None,
) -> set[str]:
    """
    在已按 posId 去重后的持仓列表上，取最多 n 个 posId（cTime 升序，先开优先）。
    """
    if max_n is None or max_n <= 0:
        return {str(p["posId"]) for p in unique}

    n = int(max_n)
    if n <= 0:
        return {str(p["posId"]) for p in unique}

    if len(unique) <= n:
        return {str(p["posId"]) for p in unique}

    sorted_pos = sorted(unique, key=_c_time_key)
    return {str(p["posId"]) for p in sorted_pos[:n]}


def _sim_eligible_pos_ids(
    positions: list[dict[str, Any]],
    max_n: int | None,
) -> set[str]:
    """
    模拟跟单只计入 n 个仓位：在对方唯一持仓里按 cTime 升序取前 n 个 posId（先开的优先），再按 posId 稳定。
    max_n 为 None 或 <=0 时不限制（全部可跟）。
    对 posId 去重后再计数/切片，避免 API 重复行导致「跟了 n+1 个仓位」。
    快照与开平仓事件仍使用全量持仓，不受此集合影响。
    """
    return _sim_eligible_from_unique(_unique_positions_by_pos_id(positions), max_n)


def _norm_row(p: dict[str, Any]) -> dict[str, Any]:
    lev = str(p.get("lever", "")).strip()
    if not lev:
        lev = pick_lever_from_pos(p)
    ur = p.get("uplRatio")
    if ur is None:
        ur = p.get("upl_ratio")
    upl_ratio_s = "" if ur is None else str(ur).strip()
    ul = p.get("upl")
    if ul is None:
        ul = p.get("UPL")
    upl_s = "" if ul is None else str(ul).strip()
    return {
        "posId": str(p.get("posId", "")),
        "cTime": str(p.get("cTime", "")),
        "cTime_format": str(p.get("cTime_format", "")),
        "posCcy": str(p.get("posCcy", "")),
        "posSide": str(p.get("posSide", "")),
        "lever": lev,
        "avgPx": str(p.get("avgPx", "")),
        "last": str(p.get("last", "")),
        "uplRatio": upl_ratio_s,
        "upl": upl_s,
        "pos": str(p.get("pos", "")).strip(),
        "notionalUsd": str(p.get("notionalUsd", "")).strip(),
        "notionalCcy": str(p.get("notionalCcy", "")).strip(),
        "notional": str(p.get("notional", "")).strip(),
        "margin": str(p.get("margin", "")).strip(),
        "mgnRatio": str(p.get("mgnRatio", "")).strip(),
        "liqPx": str(p.get("liqPx", "")).strip(),
    }


def _row_src_metrics(row: dict[str, Any]) -> tuple[str | None, str | None, str | None, str | None]:
    """社区持仓：持仓量、保证金、维持保证金率、预估强平价（写入模拟行/刷新）。"""

    def g(key: str) -> str | None:
        v = row.get(key)
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    return g("pos"), g("margin"), g("mgnRatio"), g("liqPx")


def _apply_src_metrics_to_rec(
    rec: FollowSimRecord,
    row: dict[str, Any],
) -> None:
    sp, sm, smr, slx = _row_src_metrics(row)
    if sp is not None:
        rec.src_pos = sp
    if sm is not None:
        rec.src_margin = sm
    if smr is not None:
        rec.src_mgn_ratio = smr
    if slx is not None:
        rec.src_liq_px = slx


def _to_dec(s: str | None) -> Decimal:
    if s is None or str(s).strip() == "":
        return Decimal(0)
    try:
        return Decimal(str(s).strip())
    except Exception:
        return Decimal(0)


def _row_notional_usd(row: dict[str, Any]) -> Decimal:
    for k in ("notionalUsd", "notional", "notionalCcy"):
        v = row.get(k)
        if v is None or str(v).strip() == "":
            continue
        try:
            d = Decimal(str(v).strip())
            if d != 0:
                return abs(d)
        except Exception:
            continue
    pos = _to_dec(str(row.get("pos", "")).strip() or None)
    last = _to_dec(str(row.get("last", "")).strip() or None)
    if pos == 0 or last == 0:
        return Decimal(0)
    return abs(pos * last)


def _sim_pnl_usdt(
    stake: Decimal,
    entry_s: str | None,
    px_s: str | None,
    side: str | None,
) -> Decimal:
    """按成本 stake：多 (mark-entry)/entry*stake，空 (entry-mark)/entry*stake。"""
    entry = _to_dec(entry_s)
    px = _to_dec(px_s)
    if entry <= 0:
        return Decimal(0)
    s = (side or "").lower()
    if s == "short":
        return stake * (entry - px) / entry
    return stake * (px - entry) / entry


def _should_emit_live_open(acc: FollowAccount) -> bool:
    if not acc.live_trading_enabled:
        return False
    if acc.okx_api_account_id is None:
        return False
    coeff = (
        Decimal(str(acc.open_by_asset_ratio_coeff))
        if acc.open_by_asset_ratio_coeff is not None
        else Decimal("1")
    )
    return coeff > 0


def _append_live_follow_open_intent(
    acc: FollowAccount,
    sim_id: int,
    row: dict[str, Any],
    pid: str,
    open_intents: list[LiveFollowOpenIntent],
    *,
    source_equity_usdt: Decimal | None,
) -> None:
    if not _should_emit_live_open(acc):
        return
    ccy = (row.get("posCcy") or "").strip()
    if not ccy:
        return
    ps = (row.get("posSide") or "").strip().lower()
    if ps not in ("long", "short"):
        return
    coeff = (
        Decimal(str(acc.open_by_asset_ratio_coeff))
        if acc.open_by_asset_ratio_coeff is not None
        else Decimal("1")
    )
    try:
        src_pos = abs(Decimal(str(row.get("pos") or "0").strip()))
    except Exception:
        src_pos = Decimal(0)
    if coeff <= 0 or src_pos <= 0:
        return
    contracts = (src_pos * coeff).normalize()
    contracts_s = format(contracts, "f").rstrip("0").rstrip(".")
    if not contracts_s:
        return
    lev_s = str(row.get("lever") or "").strip()
    oid = acc.okx_api_account_id
    if oid is None:
        return
    open_intents.append(
        LiveFollowOpenIntent(
            follow_account_id=acc.id,
            okx_api_account_id=oid,
            sim_record_id=sim_id,
            pos_id=pid,
            inst_id=normalize_swap_inst_id(ccy),
            pos_side=ps,
            lever_str=lev_s if lev_s else None,
            contracts=contracts_s,
        )
    )


def _create_sim_open(
    db: Session,
    acc: FollowAccount,
    row: dict[str, Any],
    pid: str,
    *,
    open_ev: FollowPositionEvent | None = None,
) -> int | None:
    if _has_open_sim(db, acc.id, pid):
        return None
    stake = (
        acc.bet_amount_per_position
        if acc.bet_amount_per_position is not None
        else Decimal(0)
    )
    mark = row.get("last") or "0"
    entry = row.get("avgPx") or "0"
    side = row.get("posSide")
    ur = _sim_pnl_usdt(stake, str(entry) if entry else None, str(mark) if mark else None, side)
    now = now_cn()
    sp, sm, smr, slx = _row_src_metrics(row)
    rec = FollowSimRecord(
        follow_account_id=acc.id,
        pos_id=pid,
        pos_ccy=row.get("posCcy") or None,
        pos_side=side or None,
        entry_avg_px=str(entry) if entry else None,
        stake_usdt=stake,
        status="open",
        open_event_id=open_ev.id if open_ev else None,
        unrealized_pnl_usdt=ur,
        last_mark_px=str(mark) if mark else None,
        updated_at=now,
        src_pos=sp,
        src_margin=sm,
        src_mgn_ratio=smr,
        src_liq_px=slx,
        total_invested_usdt=stake,
    )
    db.add(rec)
    db.flush()
    return rec.id


def _close_sim_at_exit(
    db: Session,
    acc: FollowAccount,
    pid: str,
    exit_row: dict[str, Any],
    close_ev: FollowPositionEvent | None,
    close_intents: list[LiveFollowCloseIntent],
) -> None:
    rec = db.execute(
        select(FollowSimRecord)
        .where(
            FollowSimRecord.follow_account_id == acc.id,
            FollowSimRecord.pos_id == pid,
            FollowSimRecord.status == "open",
        )
        .order_by(FollowSimRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if rec is None:
        return
    want_live_close = (
        rec.live_open_ok is True
        and acc.live_trading_enabled
        and acc.okx_api_account_id is not None
    )
    ccy_for_close = (rec.pos_ccy or "").strip()
    inst_close = normalize_swap_inst_id(ccy_for_close) if ccy_for_close else ""
    oid_close = acc.okx_api_account_id
    exit_px = exit_row.get("last") or exit_row.get("avgPx") or "0"
    realized = _sim_pnl_usdt(
        rec.stake_usdt,
        rec.entry_avg_px,
        str(exit_px) if exit_px else None,
        rec.pos_side,
    )
    now = now_cn()
    rec.status = "closed"
    rec.realized_pnl_usdt = realized
    rec.unrealized_pnl_usdt = Decimal(0)
    rec.exit_px = str(exit_px) if exit_px else None
    rec.last_mark_px = str(exit_px) if exit_px else None
    rec.close_event_id = close_ev.id if close_ev else None
    rec.closed_at = now
    rec.updated_at = now
    _apply_src_metrics_to_rec(rec, exit_row)

    if want_live_close and inst_close and oid_close is not None:
        close_intents.append(
            LiveFollowCloseIntent(
                follow_account_id=acc.id,
                okx_api_account_id=oid_close,
                sim_record_id=rec.id,
                inst_id=inst_close,
                pos_side=rec.pos_side,
            )
        )


def _has_open_sim(db: Session, acc_id: int, pid: str) -> bool:
    r = db.execute(
        select(FollowSimRecord.id)
        .where(
            FollowSimRecord.follow_account_id == acc_id,
            FollowSimRecord.pos_id == pid,
            FollowSimRecord.status == "open",
        )
        .limit(1)
    ).scalar_one_or_none()
    return r is not None


def _same_source_position_recently_closed(
    db: Session,
    acc_id: int,
    pid: str,
    row: dict[str, Any],
) -> bool:
    """
    若该 posId 最近一条模拟记录已平仓，且其 close_event 里的 cTime 与当前社区仓位一致，
    说明是“同一笔源仓位”被手动平掉，此时不应立即自动补开。
    """
    rec = db.execute(
        select(FollowSimRecord)
        .where(
            FollowSimRecord.follow_account_id == acc_id,
            FollowSimRecord.pos_id == pid,
            FollowSimRecord.status == "closed",
        )
        .order_by(FollowSimRecord.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    # 仅把「手动关闭跟单」视为关闭标记；其余 closed（如自然平仓）不阻止默认开启跟单。
    if rec is None or rec.close_event_id is None or rec.live_close_ok is not True:
        return False
    ev = db.get(FollowPositionEvent, rec.close_event_id)
    if ev is None or not ev.detail_json:
        return False
    try:
        d = json.loads(ev.detail_json)
    except Exception:
        return False
    if not isinstance(d, dict):
        return False
    old_ct = str(d.get("cTime", "")).strip()
    new_ct = str(row.get("cTime", "")).strip()
    return bool(old_ct) and old_ct == new_ct


def _pid_manually_closed(db: Session, acc_id: int, pid: str) -> bool:
    """
    同一 pos_id 的最新一条记录若为 closed 且 live_close_ok=True，
    视为「手动关闭跟单」，在手动重新开启前禁止自动重开。
    """
    rec = (
        db.execute(
            select(FollowSimRecord)
            .where(
                FollowSimRecord.follow_account_id == acc_id,
                FollowSimRecord.pos_id == pid,
            )
            .order_by(FollowSimRecord.id.desc())
            .limit(1)
        )
        .scalar_one_or_none()
    )
    return bool(rec is not None and rec.status == "closed" and rec.live_close_ok is True)


def _reconcile_sim_follow_set(
    db: Session,
    acc: FollowAccount,
    new_map: dict[str, dict[str, Any]],
    eligible: set[str],
    close_intents: list[LiveFollowCloseIntent],
    open_intents: list[LiveFollowOpenIntent],
    adjust_intents: list[LiveFollowAdjustIntent],
    *,
    skip_open_pids: frozenset[str] = frozenset(),
    old_map: dict[str, dict[str, Any]],
    source_equity_usdt: Decimal | None,
) -> None:
    """仓位仍在对方快照中但掉出「可跟 n」时结算模拟；新进 n 且无模拟行时补开模拟。

    skip_open_pids：本事务内已在「快照 open 分支」创建模拟并发过实盘开仓 intent 的 posId，
    此处不得再补开，否则同一轮会对同一仓位跟两次（重复下单）。
    """
    acc_id = acc.id
    open_rows = (
        db.execute(
            select(FollowSimRecord).where(
                FollowSimRecord.follow_account_id == acc_id,
                FollowSimRecord.status == "open",
            )
        )
        .scalars()
        .all()
    )
    for rec in open_rows:
        pid = rec.pos_id
        if pid not in new_map:
            continue
        if pid not in eligible:
            _close_sim_at_exit(db, acc, pid, new_map[pid], None, close_intents)
            continue
        new_row = new_map.get(pid)
        if not isinstance(new_row, dict):
            continue
        inst_id = normalize_swap_inst_id((rec.pos_ccy or "").strip() or str(new_row.get("posCcy", "")))
        ps = (rec.pos_side or str(new_row.get("posSide", ""))).strip().lower()
        if not inst_id or ps not in ("long", "short") or acc.okx_api_account_id is None:
            continue
        lever_s = str(new_row.get("lever", "")).strip() or None
        coeff = (
            Decimal(str(acc.open_by_asset_ratio_coeff))
            if acc.open_by_asset_ratio_coeff is not None
            else Decimal("1")
        )
        if coeff <= 0:
            continue
        try:
            src_pos = abs(Decimal(str(new_row.get("pos") or "0").strip()))
        except Exception:
            src_pos = Decimal(0)
        if src_pos <= 0:
            continue
        target_contracts_s = format(src_pos * coeff, "f").rstrip("0").rstrip(".")
        if not target_contracts_s:
            continue
        adjust_intents.append(
            LiveFollowAdjustIntent(
                follow_account_id=acc.id,
                okx_api_account_id=acc.okx_api_account_id,
                sim_record_id=rec.id,
                inst_id=inst_id,
                pos_side=ps,
                lever_str=lever_s,
                action="rebalance",
                contracts=target_contracts_s,
            )
        )

    for pid in eligible:
        if pid not in new_map:
            continue
        if pid in skip_open_pids:
            continue
        if _pid_manually_closed(db, acc_id, pid):
            continue
        if _same_source_position_recently_closed(db, acc_id, pid, new_map[pid]):
            continue
        if not _has_open_sim(db, acc_id, pid):
            sid = _create_sim_open(db, acc, new_map[pid], pid, open_ev=None)
            if sid is not None:
                _append_live_follow_open_intent(
                    acc,
                    sid,
                    new_map[pid],
                    pid,
                    open_intents,
                    source_equity_usdt=source_equity_usdt,
                )


def _refresh_sim_unrealized(
    db: Session,
    acc_id: int,
    new_map: dict[str, dict[str, Any]],
) -> None:
    now = now_cn()
    rows = (
        db.execute(
            select(FollowSimRecord).where(
                FollowSimRecord.follow_account_id == acc_id,
                FollowSimRecord.status == "open",
            )
        )
        .scalars()
        .all()
    )
    for r in rows:
        pid = r.pos_id
        if pid not in new_map:
            continue
        row = new_map[pid]
        mark = row.get("last") or "0"
        r.last_mark_px = str(mark) if mark else None
        r.unrealized_pnl_usdt = _sim_pnl_usdt(
            r.stake_usdt,
            r.entry_avg_px,
            str(mark) if mark else None,
            r.pos_side,
        )
        _apply_src_metrics_to_rec(r, row)
        r.updated_at = now


def _apply_snapshot_and_events(
    db: Session,
    acc: FollowAccount,
    positions: list[dict[str, Any]],
    *,
    close_intents: list[LiveFollowCloseIntent],
    open_intents: list[LiveFollowOpenIntent],
    adjust_intents: list[LiveFollowAdjustIntent],
    source_equity_usdt: Decimal | None,
) -> None:
    unique_rows = _unique_positions_by_pos_id(positions)
    new_map = {str(p["posId"]): _norm_row(p) for p in unique_rows if p.get("posId") is not None}
    eligible = _sim_eligible_from_unique(unique_rows, acc.max_follow_positions)
    snap = db.get(FollowPositionSnapshot, acc.id)
    un = acc.unique_name or ""

    if snap is None:
        old_map: dict[str, dict[str, Any]] = {}
    else:
        try:
            old_map = json.loads(snap.snapshot_json)
        except Exception:
            old_map = {}

    open_branch_handled_pids: set[str] = set()
    for pid, row in new_map.items():
        if _pid_manually_closed(db, acc.id, pid):
            continue
        if pid not in old_map:
            ev = FollowPositionEvent(
                follow_account_id=acc.id,
                unique_name=un,
                event_type="open",
                pos_id=pid,
                pos_ccy=row.get("posCcy"),
                pos_side=row.get("posSide"),
                lever=row.get("lever"),
                avg_px=row.get("avgPx"),
                last_px=row.get("last"),
                c_time=row.get("cTime"),
                detail_json=json.dumps(row, ensure_ascii=False),
            )
            db.add(ev)
            db.flush()
            if pid in eligible:
                sid = _create_sim_open(db, acc, row, pid, open_ev=ev)
                if sid is not None:
                    open_branch_handled_pids.add(pid)
                    _append_live_follow_open_intent(
                        acc,
                        sid,
                        row,
                        pid,
                        open_intents,
                        source_equity_usdt=source_equity_usdt,
                    )

    for pid, old_row in old_map.items():
        if pid not in new_map:
            ev = FollowPositionEvent(
                follow_account_id=acc.id,
                unique_name=un,
                event_type="close",
                pos_id=pid,
                pos_ccy=old_row.get("posCcy"),
                pos_side=old_row.get("posSide"),
                lever=old_row.get("lever"),
                avg_px=old_row.get("avgPx"),
                last_px=old_row.get("last"),
                c_time=old_row.get("cTime"),
                detail_json=json.dumps(old_row, ensure_ascii=False),
            )
            db.add(ev)
            db.flush()
            _close_sim_at_exit(db, acc, pid, old_row, ev, close_intents)

    if snap is None:
        db.add(
            FollowPositionSnapshot(
                follow_account_id=acc.id,
                snapshot_json=json.dumps(new_map, ensure_ascii=False),
                updated_at=now_cn(),
            )
        )
    else:
        snap.snapshot_json = json.dumps(new_map, ensure_ascii=False)
        snap.updated_at = now_cn()

    db.flush()
    _reconcile_sim_follow_set(
        db,
        acc,
        new_map,
        eligible,
        close_intents,
        open_intents,
        adjust_intents,
        skip_open_pids=frozenset(open_branch_handled_pids),
        old_map=old_map,
        source_equity_usdt=source_equity_usdt,
    )
    _refresh_sim_unrealized(db, acc.id, new_map)
    db.commit()


def _sync_fetch_enabled_accounts() -> list[tuple[int, str]]:
    """在主线程外的线程中执行，避免阻塞 asyncio 事件循环。"""
    db = SessionLocal()
    try:
        rows = (
            db.execute(
                select(FollowAccount.id, FollowAccount.unique_name).where(
                    FollowAccount.enabled == True,  # noqa: E712
                    FollowAccount.unique_name.isnot(None),
                )
            )
            .all()
        )
        out: list[tuple[int, str]] = []
        for rid, un in rows:
            if un:
                out.append((int(rid), str(un)))
        return out
    finally:
        db.close()


def _sync_apply_positions(
    aid: int,
    positions: list[dict[str, Any]],
    source_equity_usdt_s: str | None,
) -> tuple[list[LiveFollowCloseIntent], list[LiveFollowOpenIntent], list[LiveFollowAdjustIntent]]:
    close_intents: list[LiveFollowCloseIntent] = []
    open_intents: list[LiveFollowOpenIntent] = []
    adjust_intents: list[LiveFollowAdjustIntent] = []
    db = SessionLocal()
    try:
        acc = db.get(FollowAccount, aid)
        if not acc or not acc.enabled or not acc.unique_name:
            return ([], [], [])
        src_eq = _to_dec(source_equity_usdt_s)
        if src_eq <= 0:
            src_eq = None
        _apply_snapshot_and_events(
            db,
            acc,
            positions,
            close_intents=close_intents,
            open_intents=open_intents,
            adjust_intents=adjust_intents,
            source_equity_usdt=src_eq,
        )
        return (close_intents, open_intents, adjust_intents)
    except Exception:
        close_intents.clear()
        open_intents.clear()
        adjust_intents.clear()
        raise
    finally:
        db.close()


# 每帐户独立协程内的轮询间隔（秒）；各帐户互不影响。
_ACCOUNT_POLL_INTERVAL_SEC = 0.8
_OVERVIEW_REFRESH_INTERVAL_SEC = 5.0


async def _fetch_overview_equity(unique_name: str) -> str | None:
    overview = await OkxTrade.get_overview_data(unique_name)
    src_eq = None if not isinstance(overview, dict) else overview.get("equity")
    return None if src_eq is None else str(src_eq)


async def _account_position_loop(account_id: int, unique_name: str) -> None:
    """
    单个启用帐户的持仓轮询：异步请求欧易 + 线程池写库。
    与其它帐户并发运行；某一帐户接口变慢不会拖慢其它帐户。
    """
    last_overview_fetch_mono = 0.0
    cached_src_eq: str | None = None
    overview_task: asyncio.Task[str | None] | None = None
    intents_queue: asyncio.Queue[
        tuple[list[LiveFollowCloseIntent], list[LiveFollowOpenIntent], list[LiveFollowAdjustIntent]]
    ] = asyncio.Queue()

    async def _intents_worker() -> None:
        while True:
            closes, opens, adjusts = await intents_queue.get()
            try:
                await run_live_follow_intents(closes, opens, adjusts)
            except Exception as e:
                _log(f"run_live_follow_intents error unique_name={unique_name!r}: {e!r}")
            finally:
                intents_queue.task_done()

    intents_worker_task = asyncio.create_task(_intents_worker())
    try:
        while True:
            try:
                # 对方持仓主链路：优先拉取，不等待 overview。
                ok_raw, raw = await OkxTrade.get_position_current_safe(unique_name)
                if not ok_raw:
                    _log(f"skip tick due to bad source response unique_name={unique_name!r}")
                    await asyncio.sleep(_ACCOUNT_POLL_INTERVAL_SEC)
                    continue
                if not isinstance(raw, list):
                    raw = []

                now_mono = time.monotonic()
                need_refresh_overview = (
                    cached_src_eq is None
                    or (now_mono - last_overview_fetch_mono) >= _OVERVIEW_REFRESH_INTERVAL_SEC
                )
                if need_refresh_overview and overview_task is None:
                    overview_task = asyncio.create_task(_fetch_overview_equity(unique_name))

                if overview_task is not None and overview_task.done():
                    try:
                        cached_src_eq = overview_task.result()
                        last_overview_fetch_mono = now_mono
                    except Exception as e:
                        _log(f"overview fetch error unique_name={unique_name!r}: {e!r}")
                    finally:
                        overview_task = None

                closes, opens, adjusts = await asyncio.to_thread(
                    _sync_apply_positions,
                    account_id,
                    raw,
                    cached_src_eq,
                )
                _log(
                    f"tick follow_id={account_id} unique_name={unique_name!r} "
                    f"positions={len(raw)} close={len(closes)} open={len(opens)} adjust={len(adjusts)}"
                )
                if closes or opens or adjusts:
                    await intents_queue.put((closes, opens, adjusts))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                _log(f"api error unique_name={unique_name!r}: {e!r}")
            await asyncio.sleep(_ACCOUNT_POLL_INTERVAL_SEC)
    finally:
        if overview_task is not None and not overview_task.done():
            overview_task.cancel()
            await asyncio.gather(overview_task, return_exceptions=True)
        intents_worker_task.cancel()
        await asyncio.gather(intents_worker_task, return_exceptions=True)


async def position_monitor_loop() -> None:
    """
    对已启用跟单帐户：每个帐户单独协程并发轮询（欧易接口并发、写库各用独立 Session）。
    主管协程每秒对齐一次 DB 中的启用列表，以便启用/停用及时生效。
    """
    tasks: dict[int, asyncio.Task] = {}
    un_by_aid: dict[int, str] = {}
    while True:
        try:
            if not db_config.MYSQL_DB:
                for t in tasks.values():
                    t.cancel()
                if tasks:
                    await asyncio.gather(*tasks.values(), return_exceptions=True)
                tasks.clear()
                un_by_aid.clear()
                await asyncio.sleep(5)
                continue

            accounts = await asyncio.to_thread(_sync_fetch_enabled_accounts)
            want: dict[int, str] = {aid: un for aid, un in accounts}

            for aid, t in list(tasks.items()):
                if aid not in want or un_by_aid.get(aid) != want[aid]:
                    t.cancel()

            for aid, t in list(tasks.items()):
                if aid not in want or un_by_aid.get(aid) != want[aid]:
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                    tasks.pop(aid, None)
                    un_by_aid.pop(aid, None)

            for aid, un in want.items():
                if aid not in tasks:
                    tasks[aid] = asyncio.create_task(_account_position_loop(aid, un))
                    un_by_aid[aid] = un

            _log(f"supervisor tick active_tasks={len(tasks)} enabled_accounts={len(want)}")
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _log(f"supervisor error: {e!r}")
            await asyncio.sleep(2)
