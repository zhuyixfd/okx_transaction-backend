from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_serializer

from config.cn_time import as_beijing


class PositionEventOut(BaseModel):
    id: int
    follow_account_id: int
    unique_name: str
    event_type: str = Field(..., description="open=新 posId 出现 | close=posId 消失")
    pos_id: Optional[str] = None
    pos_ccy: Optional[str] = None
    pos_side: Optional[str] = None
    lever: Optional[str] = None
    avg_px: Optional[str] = None
    last_px: Optional[str] = None
    upl_ratio: Optional[str] = Field(
        None,
        description="从 detail_json 解析的 uplRatio（新写入的事件才有；历史行可能为空）",
    )
    c_time: Optional[str] = None
    detail_json: Optional[str] = None
    created_at: datetime

    @field_serializer("created_at")
    def _dt_beijing(self, v: datetime) -> datetime:
        out = as_beijing(v)
        assert out is not None
        return out


class PositionEventPageOut(BaseModel):
    items: List[PositionEventOut]
    total: int = Field(..., description="满足条件的记录总数（用于分页）")
