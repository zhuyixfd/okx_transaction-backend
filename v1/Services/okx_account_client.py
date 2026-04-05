"""
从数据库 okx_api_accounts 解析密钥并构造 module.follow_order.OkxFollowOrderClient。

manual-okx、跟单详情「绑定帐户」代理接口等统一走此模块，实际 HTTP 与签名均在 follow_order.OkxFollowOrderClient 内完成。
"""

from __future__ import annotations

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from module.follow_order import OkxFollowOrderClient, okx_client_for_db_secrets
from v1.Models.okx_api_account import OkxApiAccount


def require_okx_client(db: Session, okx_api_account_id: int) -> OkxFollowOrderClient:
    row = db.get(OkxApiAccount, okx_api_account_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="okx_api_account 不存在")
    client = okx_client_for_db_secrets(row.api_key, row.api_secret, row.api_passphrase)
    if not client.is_configured():
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="该 OKX API 帐户密钥不完整",
        )
    return client
