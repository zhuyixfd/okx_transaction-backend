"""模拟跟单资金记录：开仓生成行，平仓结算已实现盈亏；未平仓行随标记价更新浮动盈亏。

对方仓位指标与真实跟单标记见 backend/sql/init.sql 中 ALTER 注释。
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from config.db import Base


class FollowSimRecord(Base):
    __tablename__ = "follow_sim_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    follow_account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("follow_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    pos_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pos_ccy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pos_side: Mapped[str | None] = mapped_column(String(16), nullable=True)
    entry_avg_px: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stake_usdt: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False, default=Decimal("0"))

    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # open | closed

    open_event_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("follow_position_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    close_event_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("follow_position_events.id", ondelete="SET NULL"),
        nullable=True,
    )

    exit_px: Mapped[str | None] = mapped_column(String(64), nullable=True)
    realized_pnl_usdt: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    unrealized_pnl_usdt: Mapped[Decimal] = mapped_column(
        Numeric(24, 8), nullable=False, default=Decimal("0")
    )
    last_mark_px: Mapped[str | None] = mapped_column(String(64), nullable=True)

    src_pos: Mapped[str | None] = mapped_column(String(64), nullable=True)
    src_margin: Mapped[str | None] = mapped_column(String(64), nullable=True)
    src_mgn_ratio: Mapped[str | None] = mapped_column(String(64), nullable=True)
    src_liq_px: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 真实跟单：NULL=未走私有下单或仅模拟；True/False=开仓/平仓 API 最终结果（平仓仅在开仓成功时触发）。
    live_open_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    live_close_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
