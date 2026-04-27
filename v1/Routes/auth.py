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

from config.constant import config as db_config
from config.db import SessionLocal, get_db
from v1.Models.user import User
from v1.Schema.auth import LoginRequest, LoginResponse, MeResponse

router = APIRouter(prefix="/auth", tags=["auth"])


def ensure_mysql_db_configured() -> None:
    if not db_config.database_url:
        raise HTTPException(
            status_code=status.HTTP_424_FAILED_DEPENDENCY,
            detail="数据库未配置：请检查 backend/.env 后重启",
        )


DEFAULT_BOOTSTRAP_USERNAME = "admin"
DEFAULT_BOOTSTRAP_PASSWORD = "admin123"


def ensure_default_admin_user() -> None:
    """
    init_db / 建表之后：若不存在用户名为 admin 的帐号，则创建一条。
    密码与 DEFAULT_BOOTSTRAP_PASSWORD 一致，哈希方式与 /auth/login 相同。
    """
    if not db_config.database_url:
        return
    db = SessionLocal()
    try:
        existing = db.execute(
            select(User).where(User.username == DEFAULT_BOOTSTRAP_USERNAME)
        ).scalar_one_or_none()
        if existing is not None:
            return
        salt = secrets.token_hex(16)
        pwd_hash = pbkdf2_hash_password(DEFAULT_BOOTSTRAP_PASSWORD, salt=salt)
        db.add(
            User(
                username=DEFAULT_BOOTSTRAP_USERNAME,
                salt=salt,
                password_hash=pwd_hash,
            )
        )
        db.commit()
        print(
            f"[startup] 已自动创建默认用户 {DEFAULT_BOOTSTRAP_USERNAME!r}，"
            f"初始密码 {DEFAULT_BOOTSTRAP_PASSWORD!r}，生产环境请尽快修改"
        )
    except Exception as e:
        db.rollback()
        print(f"[startup] ensure_default_admin_user 失败: {e!r}")
    finally:
        db.close()


def pbkdf2_hash_password(password: str, salt: str, iterations: int = 100_000) -> str:
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations
    )
    return base64.b64encode(dk).decode("ascii")


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

