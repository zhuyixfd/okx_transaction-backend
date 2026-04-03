from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_serializer

from config.cn_time import as_beijing


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1, max_length=256)


class LoginResponse(BaseModel):
    token: str
    token_type: str = "Bearer"


class MeResponse(BaseModel):
    id: int
    username: str
    created_at: datetime

    @field_serializer("created_at")
    def _dt_beijing(self, v: datetime) -> datetime:
        out = as_beijing(v)
        assert out is not None
        return out

