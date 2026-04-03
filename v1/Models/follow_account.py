from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from config.db import Base


class FollowAccount(Base):
    """
    跟单帐户表：落地页 URL、展示昵称、OKX uniqueName、添加时间、是否启用。
    """

    __tablename__ = "follow_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    link: Mapped[str] = mapped_column(String(512), unique=True, nullable=False, index=True)
    nickname: Mapped[str | None] = mapped_column(String(256), nullable=True)
    unique_name: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_enabled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # 跟单配置（单笔帐户）
    bet_amount_per_position: Mapped[Decimal | None] = mapped_column(Numeric(24, 8), nullable=True)
    # 最多同时跟 n 个仓位：对方少于 n 则全跟；多于 n 则只跟 n；对方换仓时动态仍不超过 n。
    max_follow_positions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bet_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="cost")
    margin_ratio_threshold_pct: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, default=Decimal("200")
    )
    margin_add_ratio_of_bet: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0.2")
    )
    margin_auto_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
