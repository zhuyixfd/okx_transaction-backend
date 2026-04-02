from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from config.db import get_db
from config.constant import config as db_config
from v1.Models.user import User
from v1.Schema.auth import LoginRequest, LoginResponse, MeResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def ensure_mysql_db_configured() -> None:
    if not db_config.MYSQL_DB:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="MYSQL_DB 未配置：请在 backend/.env 中填写要使用的数据库名后重启",
        )


def pbkdf2_hash_password(password: str, salt: str, iterations: int = 100_000) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    )
    return base64.b64encode(dk).decode("ascii")


def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_hex(16)
    password_hash = pbkdf2_hash_password(password, salt=salt)
    return salt, password_hash


def verify_password(password: str, *, salt: str, password_hash: str) -> bool:
    expected = pbkdf2_hash_password(password, salt=salt)
    # Constant-time compare to avoid timing leaks.
    return hmac.compare_digest(expected, password_hash)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def create_access_token(user_id: int, secret: str, *, expires_in_seconds: int) -> str:
    exp = int(time.time()) + int(expires_in_seconds)
    payload = f"{user_id}|{exp}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest().encode(
        "ascii"
    )
    return f"{_b64url_encode(payload)}.{_b64url_encode(sig)}"


def decode_access_token(token: str, secret: str) -> Optional[int]:
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload = _b64url_decode(payload_b64)
        sig = _b64url_decode(sig_b64)

        expected_sig = hmac.new(
            secret.encode("utf-8"), payload, hashlib.sha256
        ).hexdigest().encode("ascii")

        if not hmac.compare_digest(expected_sig, sig):
            return None

        user_id_s, exp_s = payload.decode("utf-8").split("|", 1)
        exp = int(exp_s)
        if time.time() > exp:
            return None
        return int(user_id_s)
    except Exception:
        return None


def get_secret() -> str:
    # For real deployments, set AUTH_SECRET in environment.
    return os.getenv("AUTH_SECRET", "dev-change-me")


def get_access_token_from_header(
    authorization: Optional[str] = Header(default=None),
) -> str:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 Authorization 头")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authorization 格式应为 Bearer token")
    return parts[1]


def get_current_user(
    token: str = Depends(get_access_token_from_header),
    db: Session = Depends(get_db),
) -> User:
    ensure_mysql_db_configured()
    user_id = decode_access_token(token, get_secret())
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="token 无效或已过期")

    row = db.execute(select(User).where(User.id == user_id)).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在")
    return row


def seed_admin_user(db: Session) -> None:
    """
    Seed a default admin user for first-time usage.
    """
    ensure_mysql_db_configured()

    username = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
    password = os.getenv("DEFAULT_ADMIN_PASSWORD", "admin123456")

    exists = db.execute(select(User).where(User.username == username)).scalar_one_or_none()
    if exists is not None:
        return

    salt, password_hash = hash_password(password)
    db.add(
        User(
            username=username,
            salt=salt,
            password_hash=password_hash,
        )
    )
    db.commit()


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    ensure_mysql_db_configured()

    row = db.execute(select(User).where(User.username == payload.username)).scalar_one_or_none()
    if row is None or not verify_password(payload.password, salt=row.salt, password_hash=row.password_hash):
        # Avoid leaking whether username exists.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
        )

    token = create_access_token(
        row.id,
        get_secret(),
        expires_in_seconds=7 * 24 * 3600,
    )
    return LoginResponse(token=token)


@router.get("/me", response_model=MeResponse)
def me(current: User = Depends(get_current_user)) -> MeResponse:
    return MeResponse(
        id=current.id,
        username=current.username,
        created_at=current.created_at,
    )

