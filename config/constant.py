
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class DbConfig(BaseSettings):
    """
    Database configuration loaded from `.env` (project root).

    Note: this repo previously referenced `POSTGRES_*`; now it's switched to MySQL.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    MYSQL_SERVER: str = "localhost"
    MYSQL_PORT: int = 3306
    MYSQL_USER: str = "root"
    MYSQL_PASSWORD: str = ""
    MYSQL_DB: Optional[str] = ""

    MYSQL_CHARSET: str = "utf8mb4"
    MYSQL_CONNECT_TIMEOUT: int = 10

    # Used by SQLAlchemy engine.
    MYSQL_POOL_PRE_PING: bool = True
    SQLITE_PATH: str = "data/app.db"

    @property
    def mysql_url(self) -> str:
        user = quote_plus(self.MYSQL_USER)
        password = quote_plus(self.MYSQL_PASSWORD)

        db_part = f"/{self.MYSQL_DB}" if self.MYSQL_DB else "/"
        return (
            "mysql+pymysql://"
            f"{user}:{password}@{self.MYSQL_SERVER}:{int(self.MYSQL_PORT)}"
            f"{db_part}"
            f"?charset={self.MYSQL_CHARSET}"
            f"&connect_timeout={int(self.MYSQL_CONNECT_TIMEOUT)}"
        )

    @property
    def database_backend(self) -> str:
        return "mysql" if bool(self.MYSQL_DB) else "sqlite"

    @property
    def sqlite_url(self) -> str:
        p = str(self.SQLITE_PATH or "data/app.db").strip()
        if not p:
            p = "data/app.db"
        if os.path.isabs(p):
            return f"sqlite:///{p}"
        base_dir = os.path.dirname(os.path.dirname(__file__))
        return f"sqlite:///{os.path.join(base_dir, p)}"

    @property
    def database_url(self) -> str:
        return self.mysql_url if self.database_backend == "mysql" else self.sqlite_url


config = DbConfig()
