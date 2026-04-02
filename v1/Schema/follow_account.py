from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, HttpUrl, Field


class FollowAccountCreate(BaseModel):
    link: HttpUrl = Field(..., description="Follow account url")


class FollowAccountOut(BaseModel):
    id: int
    link: HttpUrl
    created_at: datetime


class FollowAccountDeleteOut(BaseModel):
    ok: bool
    id: Optional[int] = None
