from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_serializer

from config.cn_time import as_beijing

# 所有 GET 列表/单条对密钥类字段统一返回该掩码（不泄露明文）
OKX_SECRET_MASK = "********"


class OkxApiAccountCreate(BaseModel):
    okx_follow_api_key: str = Field(..., min_length=1, max_length=256)
    okx_follow_secret_key: str = Field(..., min_length=1)
    okx_follow_passphrase: str = Field(..., min_length=1, max_length=512)
    okx_follow_api_label: Optional[str] = Field(None, max_length=256)
    remark: Optional[str] = Field(None, max_length=512)


class OkxApiAccountPatch(BaseModel):
    okx_follow_api_key: Optional[str] = Field(None, max_length=256)
    okx_follow_secret_key: Optional[str] = None
    okx_follow_passphrase: Optional[str] = Field(None, max_length=512)
    okx_follow_api_label: Optional[str] = Field(None, max_length=256)
    remark: Optional[str] = Field(None, max_length=512)


class OkxApiAccountOut(BaseModel):
    id: int
    okx_follow_api_key: str = Field(description="始终为掩码")
    okx_follow_secret_key: str = Field(description="始终为掩码")
    okx_follow_passphrase: str = Field(description="始终为掩码")
    okx_follow_api_label: Optional[str] = None
    remark: Optional[str] = None
    created_at: datetime
    bound_follow_account_id: Optional[int] = Field(
        None, description="若已绑定某交易员跟单帐户，为其 follow_accounts.id"
    )
    bound_trader_label: Optional[str] = Field(
        None, description="已绑定交易员展示用：优先昵称，否则 uniqueName"
    )

    @field_serializer("created_at")
    def _dt_beijing(self, v: datetime) -> datetime:
        return as_beijing(v)


class OkxApiAccountDeleteOut(BaseModel):
    ok: bool
    id: Optional[int] = None


class FollowAccountOkxBindPatch(BaseModel):
    okx_api_account_id: Optional[int] = Field(
        None,
        description="绑定的 okx_api_accounts.id；置 null 表示解除绑定（仅允许在跟单未启用时）",
    )
