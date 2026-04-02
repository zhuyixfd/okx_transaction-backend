
from __future__ import annotations

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


config = DbConfig()
