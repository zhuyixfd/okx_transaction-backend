from __future__ import annotations

from pydantic import BaseModel, Field


class PnlTotalsBlock(BaseModel):
    """与跟单记录汇总字段一致；金额均为字符串便于前端展示。"""

    total_pnl_usdt: str = Field(..., description="总收益（USDT）")
    realized_sum_usdt: str = Field(..., description="已实现（USDT）")
    unrealized_sum_usdt: str = Field(..., description="浮动（USDT）")


class PositionPnlSummaryOut(BaseModel):
    """
    holdings：仅当前快照持仓（无已实现，总收益=浮动）。
    ledger：全部平仓事件累计已实现 + 当前快照浮动（与跟单记录分项含义一致）。
    均按帐户「每个仓位下注金额」估算，与模拟跟单公式一致。
    """

    holdings: PnlTotalsBlock
    ledger: PnlTotalsBlock
