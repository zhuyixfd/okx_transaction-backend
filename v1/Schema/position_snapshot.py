from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer

from config.cn_time import as_beijing


class PositionSnapshotItem(BaseModel):
    """监控快照中的单条持仓；last_px 为标记价（随轮询更新）。"""

    pos_id: str
    c_time: Optional[str] = None
    c_time_format: Optional[str] = None
    pos_ccy: Optional[str] = None
    pos_side: Optional[str] = None
    lever: Optional[str] = None
    avg_px: Optional[str] = None
    last_px: Optional[str] = None


class PositionSnapshotOut(BaseModel):
    unique_name: str
    refreshed_at: Optional[datetime] = Field(
        None, description="最近一次从 OKX 拉取并写入本快照的时间（北京时间）"
    )
    positions: List[PositionSnapshotItem] = Field(default_factory=list)

    @field_serializer("refreshed_at")
    def _dt_beijing(self, v: datetime | None) -> datetime | None:
        return as_beijing(v) if v is not None else None
