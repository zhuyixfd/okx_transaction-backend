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

    bet_amount_per_position: Optional[Decimal] = Field(
        None, ge=0, description="每个仓位下注金额（USDT，作保证金追加计算基数）"
    )
    max_follow_positions: Optional[int] = Field(
        None,
        ge=1,
        le=1000,
        description="最多同时跟 n 个仓位：对方少于 n 则全跟，多于 n 则只跟 n，换仓后仍不超过 n",
    )
    bet_mode: Optional[str] = Field(None, description="下注模式：cost=按成本下单")
    margin_add_ratio_of_bet: Optional[Decimal] = Field(
        None, ge=0, le=1, description="追加金额 = 下注金额 × 该比例"
    )
    margin_auto_enabled: Optional[bool] = Field(
        None, description="是否启用：根据 OKX 接口监控本人持仓保证金率并自动追加"
    )
    margin_add_max_times: Optional[int] = Field(
        None,
        ge=1,
        le=100_000,
        description="保证金自动追加次数上限；不传或 null 表示不限制",
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
    bet_amount_per_position: Optional[Decimal] = None
    max_follow_positions: Optional[int] = None
    bet_mode: str = Field(default="cost", description="cost=按成本下单")
    margin_add_ratio_of_bet: Decimal = Field(default=Decimal("0.2"))
    margin_auto_enabled: bool = False
    margin_add_max_times: Optional[int] = Field(
        None, description="保证金自动追加次数上限；null 表示不限制"
    )

    @field_serializer("last_enabled_at", "created_at", "positions_refreshed_at")
    def _dt_beijing(self, v: datetime | None) -> datetime | None:
        """JSON 中统一输出为北京时间（带 +08:00）。"""
        return as_beijing(v)


class FollowAccountDeleteOut(BaseModel):
    ok: bool
    id: Optional[int] = None
