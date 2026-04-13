from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl, field_serializer

from config.cn_time import as_beijing


class FollowAccountCreate(BaseModel):
    link: HttpUrl = Field(..., description="跟单落地页链接，如 https://oyidl.net/ul/4DtIo37")


class FollowAccountPatch(BaseModel):
    enabled: bool = Field(..., description="是否启用跟单")


class FollowConfigPatch(BaseModel):
    """跟单保证金与仓位相关配置（部分更新）。"""

    single_add_margin_usdt: Optional[Decimal] = Field(
        None, ge=0, description="单次增加保证金金额（USDT）"
    )
    max_follow_positions: Optional[int] = Field(
        None,
        ge=1,
        le=1000,
        description="最多同时跟 n 个仓位：对方少于 n 则全跟，多于 n 则只跟 n，换仓后仍不超过 n",
    )
    bet_mode: Optional[str] = Field(None, description="下注模式：cost=按成本下单")
    margin_add_ratio_of_bet: Optional[Decimal] = Field(
        None, ge=0, le=1, description="历史字段（兼容保留）；当前固定为 1"
    )
    margin_auto_enabled: Optional[bool] = Field(
        None, description="历史字段（兼容保留）；当前固定启用自动增加保证金"
    )
    margin_add_max_times: Optional[int] = Field(
        None,
        ge=1,
        le=100_000,
        description="增加保证金次数上限；不传或 null 表示不限制",
    )
    live_trading_enabled: Optional[bool] = Field(
        None,
        description="True=真实交易（调欧易私有接口）；False=仅模拟，不实际下单/追加",
    )
    position_size_coeff: Optional[Decimal] = Field(
        None,
        ge=0,
        description="持仓量系数（我方下单张数=对方持仓张数×该系数）",
    )
    maint_margin_ratio_threshold: Optional[Decimal] = Field(
        None, ge=0, description="维持保证金率阈值（比例值，2=200%）"
    )
    close_margin_ratio_threshold: Optional[Decimal] = Field(
        None, ge=0, description="平仓保证金率阈值（比例值，2=200%）"
    )
    take_profit_ratio: Optional[Decimal] = Field(
        None, ge=0, description="止盈收益率阈值（比例值，0.2=20%）"
    )
    stop_loss_ratio: Optional[Decimal] = Field(
        None, ge=0, description="止损收益率阈值（比例值，0.1=10%）"
    )


class FollowAccountOut(BaseModel):
    id: int
    link: HttpUrl
    nickname: Optional[str] = None
    unique_name: Optional[str] = None
    enabled: bool
    last_enabled_at: Optional[datetime] = None
    created_at: datetime
    positions_refreshed_at: Optional[datetime] = Field(
        None,
        description="持仓快照表最近一次写入时间（启用时由监控轮询更新标记价后刷新）",
    )
    single_add_margin_usdt: Optional[Decimal] = None
    max_follow_positions: Optional[int] = None
    bet_mode: str = Field(default="cost", description="cost=按成本下单")
    margin_add_ratio_of_bet: Decimal = Field(default=Decimal("1"))
    margin_auto_enabled: bool = True
    margin_add_max_times: Optional[int] = Field(
        None, description="增加保证金次数上限；null 表示不限制"
    )
    okx_api_account_id: Optional[int] = Field(
        None, description="绑定的 OKX API 帐户 id（okx_api_accounts.id）"
    )
    live_trading_enabled: bool = Field(
        False,
        description="是否启用真实交易（否则为模拟，不调欧易私有交易接口）",
    )
    position_size_coeff: Decimal = Field(
        default=Decimal("1"),
        description="持仓量系数（默认 1）",
    )
    maint_margin_ratio_threshold: Optional[Decimal] = Field(
        None, description="维持保证金率阈值（比例值，2=200%）"
    )
    close_margin_ratio_threshold: Optional[Decimal] = Field(
        None, description="平仓保证金率阈值（比例值，2=200%）"
    )
    take_profit_ratio: Optional[Decimal] = Field(
        None, description="止盈收益率阈值（比例值，0.2=20%）"
    )
    stop_loss_ratio: Optional[Decimal] = Field(
        None, description="止损收益率阈值（比例值，0.1=10%）"
    )

    @field_serializer("last_enabled_at", "created_at", "positions_refreshed_at")
    def _dt_beijing(self, v: datetime | None) -> datetime | None:
        """JSON 中统一输出为北京时间（带 +08:00）。"""
        return as_beijing(v)


class FollowAccountDeleteOut(BaseModel):
    ok: bool
    id: Optional[int] = None
