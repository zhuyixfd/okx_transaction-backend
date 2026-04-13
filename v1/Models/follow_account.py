from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, func
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
    margin_add_ratio_of_bet: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0.2")
    )
    margin_auto_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 本人持仓保证金自动追加：最多允许追加的次数；NULL 表示不限制（由监控实际执行）。
    margin_add_max_times: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 维持保证金率阈值（比例值，2=200%），仅配置存储，策略逻辑后续按需接入。
    maint_margin_ratio_threshold: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    # 平仓保证金率阈值（比例值，2=200%），仅配置存储，策略逻辑后续按需接入。
    close_margin_ratio_threshold: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    # 止盈收益率阈值（比例值，0.2=20%），仅配置存储，策略逻辑后续按需接入。
    take_profit_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    # 止损收益率阈值（比例值，0.1=10%），仅配置存储，策略逻辑后续按需接入。
    stop_loss_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

    # 绑定的 OKX API 帐户（跟单下单/保证金用）；停用跟单不清除此字段。
    okx_api_account_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("okx_api_accounts.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
    )
    # True：对绑定 OKX 帐户执行真实下单/追加保证金；False：仅模拟跟单记录，不调用私有交易接口。
    live_trading_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 历史字段保留：当前策略固定不使用按资产比例开仓。
    open_by_asset_ratio: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # 实际语义已切换为持仓量系数：我方下单张数 = 对方持仓张数 × 系数（默认 1）。
    open_by_asset_ratio_coeff: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("1")
    )
