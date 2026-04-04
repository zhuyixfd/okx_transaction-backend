from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.cn_time import now_cn
from config.constant import config as db_config
from config.db import SessionLocal
from okx import OkxTrade
from okx.trade import pick_lever_from_pos
from v1.Models.follow_account import FollowAccount
from v1.Models.follow_position import FollowPositionEvent, FollowPositionSnapshot
from v1.Models.follow_sim_record import FollowSimRecord


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
    }


def _to_dec(s: str | None) -> Decimal:
    if s is None or str(s).strip() == "":
        return Decimal(0)
    try:
        return Decimal(str(s).strip())
    except Exception:
        return Decimal(0)


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


def _create_sim_open(
    db: Session,
    acc: FollowAccount,
    row: dict[str, Any],
    pid: str,
    *,
    open_ev: FollowPositionEvent | None = None,
) -> None:
    if _has_open_sim(db, acc.id, pid):
        return
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
    db.add(
        FollowSimRecord(
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
        )
    )


def _close_sim_at_exit(
    db: Session,
    acc: FollowAccount,
    pid: str,
    exit_row: dict[str, Any],
    close_ev: FollowPositionEvent | None,
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


def _reconcile_sim_follow_set(
    db: Session,
    acc: FollowAccount,
    new_map: dict[str, dict[str, Any]],
    eligible: set[str],
) -> None:
    """仓位仍在对方快照中但掉出「可跟 n」时结算模拟；新进 n 且无模拟行时补开模拟。"""
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
            _close_sim_at_exit(db, acc, pid, new_map[pid], None)

    for pid in eligible:
        if pid not in new_map:
            continue
        if not _has_open_sim(db, acc_id, pid):
            _create_sim_open(db, acc, new_map[pid], pid, open_ev=None)


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
        r.updated_at = now


def _apply_snapshot_and_events(
    db: Session,
    acc: FollowAccount,
    positions: list[dict[str, Any]],
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

    for pid, row in new_map.items():
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
                _create_sim_open(db, acc, row, pid, open_ev=ev)

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
            _close_sim_at_exit(db, acc, pid, old_row, close_ev=ev)

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
    _reconcile_sim_follow_set(db, acc, new_map, eligible)
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


def _sync_apply_positions(aid: int, positions: list[dict[str, Any]]) -> None:
    db = SessionLocal()
    try:
        acc = db.get(FollowAccount, aid)
        if not acc or not acc.enabled or not acc.unique_name:
            return
        _apply_snapshot_and_events(db, acc, positions)
    finally:
        db.close()


# 每帐户独立协程内的轮询间隔（秒）；各帐户互不影响。
_ACCOUNT_POLL_INTERVAL_SEC = 1.0


async def _account_position_loop(account_id: int, unique_name: str) -> None:
    """
    单个启用帐户的持仓轮询：异步请求欧易 + 线程池写库。
    与其它帐户并发运行；某一帐户接口变慢不会拖慢其它帐户。
    """
    while True:
        try:
            raw = await OkxTrade.get_position_current(unique_name)
            if not isinstance(raw, list):
                raw = []
            await asyncio.to_thread(_sync_apply_positions, account_id, raw)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[position_monitor] API {unique_name!r}: {e!r}")
        await asyncio.sleep(_ACCOUNT_POLL_INTERVAL_SEC)


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

            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[position_monitor] loop: {e!r}")
            await asyncio.sleep(2)
