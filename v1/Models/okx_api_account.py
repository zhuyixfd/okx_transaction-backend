from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from config.db import Base


class OkxApiAccount(Base):
    """
    用户录入的 OKX API 跟单凭证（仅存数据库，不写 .env）。
    列表/详情接口对密钥类字段一律返回掩码，不向前端泄露明文。
    """

    __tablename__ = "okx_api_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_key: Mapped[str] = mapped_column(String(256), nullable=False)
    api_secret: Mapped[str] = mapped_column(Text, nullable=False)
    api_passphrase: Mapped[str] = mapped_column(String(512), nullable=False)
    api_label: Mapped[str | None] = mapped_column(String(256), nullable=True)
    remark: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
