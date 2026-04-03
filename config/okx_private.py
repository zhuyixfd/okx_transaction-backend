"""OKX 私有接口（交易）凭证：从环境变量读取，用于本人持仓保证金监控与调仓。"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class OkxPrivateConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    OKX_API_KEY: str = ""
    OKX_SECRET_KEY: str = ""
    OKX_PASSPHRASE: str = ""
    """为 True 时使用模拟盘（请求头 x-simulated-trading: 1）。"""
    OKX_USE_PAPER: bool = False

    def is_configured(self) -> bool:
        return bool(self.OKX_API_KEY and self.OKX_SECRET_KEY and self.OKX_PASSPHRASE)


okx_private_config = OkxPrivateConfig()
