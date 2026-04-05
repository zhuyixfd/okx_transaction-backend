from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer

from config.cn_time import as_beijing


class FollowSimRecordOut(BaseModel):
    id: int
    follow_account_id: int
    pos_id: str
    pos_ccy: Optional[str] = None
    pos_side: Optional[str] = None
    entry_avg_px: Optional[str] = None
    stake_usdt: Decimal = Field(..., description="模拟本金（USDT）")
    status: str = Field(..., description="open=跟单中 | closed=已平仓")
    exit_px: Optional[str] = None
    realized_pnl_usdt: Optional[Decimal] = Field(None, description="已平仓已实现盈亏（USDT）")
    unrealized_pnl_usdt: Decimal = Field(
        ..., description="未平仓浮动盈亏；已平仓为 0"
    )
    last_mark_px: Optional[str] = None
    src_pos: Optional[str] = Field(None, description="对方持仓量（落库同步）")
    src_margin: Optional[str] = Field(None, description="对方保证金")
    src_mgn_ratio: Optional[str] = Field(None, description="对方维持保证金率")
    src_liq_px: Optional[str] = Field(None, description="对方预估强平价")
    opened_at: datetime
    closed_at: Optional[datetime] = None
    updated_at: datetime

    @field_serializer("opened_at", "closed_at", "updated_at")
    def _dt_beijing(self, v: datetime | None) -> datetime | None:
        if v is None:
            return None
        out = as_beijing(v)
        assert out is not None
        return out

    @field_serializer("stake_usdt", "realized_pnl_usdt", "unrealized_pnl_usdt")
    def _dec_str(self, v: Decimal | None) -> str | None:
        if v is None:
            return None
        return format(v, "f")


class FollowSimRecordsPageOut(BaseModel):
    items: List[FollowSimRecordOut]
    total: int = Field(..., description="满足条件的记录总数（分页）")
    total_pnl_usdt: str = Field(
        ...,
        description="总收益（USDT）：已实现合计 + 未平仓浮动盈亏合计",
    )
    realized_sum_usdt: str = Field(..., description="已平仓已实现盈亏合计（USDT）")
    unrealized_sum_usdt: str = Field(..., description="未平仓浮动盈亏合计（USDT）")
