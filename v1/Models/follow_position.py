from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from config.db import Base


class FollowPositionSnapshot(Base):
    """
    每个跟单帐户上一次拉取到的持仓快照（JSON：posId -> 字段），用于比对变化。
    """

    __tablename__ = "follow_position_snapshots"

    follow_account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("follow_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class FollowPositionEvent(Base):
    """持仓监控记录：开仓 / 平仓 / 字段变化。"""

    __tablename__ = "follow_position_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    follow_account_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("follow_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    unique_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    pos_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    pos_ccy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    pos_side: Mapped[str | None] = mapped_column(String(16), nullable=True)
    lever: Mapped[str | None] = mapped_column(String(32), nullable=True)
    avg_px: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_px: Mapped[str | None] = mapped_column(String(64), nullable=True)
    c_time: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
