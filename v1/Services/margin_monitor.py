"""
轮询本人 OKX 永续持仓保证金率：当 mgnRatio ≤ 内置阈值（200%）时，按「下注金额 × 追加比例」追加逐仓保证金。

需配置环境变量 OKX_FOLLOW_API_KEY / OKX_FOLLOW_SECRET_KEY / OKX_FOLLOW_PASSPHRASE；
至少一条 follow_accounts 记录开启 margin_auto_enabled 且填写 bet_amount_per_position。

多帐户同时启用时：取下注金额、追加比例中的最小值（偏保守）。
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from config.constant import config as db_config
from config.db import SessionLocal
from module.follow_order import add_position_margin, follow_order_config, get_positions_inst
from v1.Models.follow_account import FollowAccount

_last_add_ts: dict[tuple[str, str], float] = {}
COOLDOWN_SEC = 60.0
# 原可配置项已移除，触发条件固定为该阈值（与历史库默认一致）
DEFAULT_MARGIN_RATIO_THRESHOLD_PCT = Decimal("200")


def _aggregate_config(db: Session) -> dict[str, Decimal] | None:
    rows = (
        db.execute(
            select(FollowAccount).where(
                FollowAccount.margin_auto_enabled == True,  # noqa: E712
                FollowAccount.enabled == True,  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return None
    bets = [
        r.bet_amount_per_position
        for r in rows
        if r.bet_amount_per_position is not None and r.bet_amount_per_position > 0
    ]
    ars = [r.margin_add_ratio_of_bet for r in rows if r.margin_add_ratio_of_bet is not None]
    if not bets:
        return None
    return {
        "bet_min": min(bets),
        "thr": DEFAULT_MARGIN_RATIO_THRESHOLD_PCT,
        "add_ratio": min(ars) if ars else Decimal("0.2"),
    }


async def margin_monitor_loop() -> None:
    while True:
        try:
            if not db_config.MYSQL_DB or not follow_order_config.is_configured():
                await asyncio.sleep(30)
                continue

            db = SessionLocal()
            try:
                agg = _aggregate_config(db)
            finally:
                db.close()

            if agg is None:
                await asyncio.sleep(25)
                continue

            ok, data = await get_positions_inst("SWAP")
            if not ok:
                print(f"[margin_monitor] get_positions: {data!r}")
                await asyncio.sleep(15)
                continue

            pos_list = data.get("data") or []
            thr_f = float(agg["thr"])
            bet_min = agg["bet_min"]
            add_ratio = agg["add_ratio"]

            for p in pos_list:
                inst_id = str(p.get("instId") or "")
                if not inst_id:
                    continue
                pos_side = (p.get("posSide") or "net").lower()
                if pos_side not in ("long", "short", "net"):
                    pos_side = "net"
                mgn_raw = p.get("mgnRatio")
                if mgn_raw is None or mgn_raw == "":
                    continue
                try:
                    mgn = float(mgn_raw)
                except (TypeError, ValueError):
                    continue
                if mgn > thr_f:
                    continue

                key = (inst_id, pos_side)
                now = time.time()
                if now - _last_add_ts.get(key, 0) < COOLDOWN_SEC:
                    continue

                add_amt: Decimal = bet_min * add_ratio
                amt_str = f"{float(add_amt):.8f}".rstrip("0").rstrip(".")
                if not amt_str or (amt_str.replace(".", "", 1).isdigit() and float(amt_str) <= 0):
                    continue

                ok2, res = await add_position_margin(inst_id, pos_side, amt_str)
                if ok2:
                    _last_add_ts[key] = now
                    print(f"[margin_monitor] add margin ok {inst_id} {pos_side} amt={amt_str}")
                else:
                    print(f"[margin_monitor] add margin fail {inst_id}: {res!r}")
                await asyncio.sleep(0.35)

            await asyncio.sleep(12)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[margin_monitor] loop: {e!r}")
            await asyncio.sleep(20)
