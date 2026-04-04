"""
OKX v5 私有接口（跟单账户）：统一使用 .env 中 OKX_FOLLOW_* 凭证。

- POST /api/v5/trade/order — 下单
  https://www.okx.com/docs-v5/zh/#order-book-trading-trade-post-place-order
- GET /api/v5/account/positions — 持仓
- POST /api/v5/account/position/margin-balance — 逐仓调整保证金（追加/减少）

控制台 IP 白名单、备注名仅本地备注，不参与签名。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import aiohttp
from pydantic_settings import BaseSettings, SettingsConfigDict

_OKX_REST = "https://www.okx.com"
_PLACE_ORDER_PATH = "/api/v5/trade/order"
_MARGIN_BALANCE_PATH = "/api/v5/account/position/margin-balance"


class FollowOrderConfig(BaseSettings):
    """跟单账户 OKX API：下单、查持仓、调整保证金均读此配置。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    OKX_FOLLOW_API_KEY: str = ""
    OKX_FOLLOW_SECRET_KEY: str = ""
    OKX_FOLLOW_PASSPHRASE: str = ""
    """为 True 时加请求头 x-simulated-trading: 1（模拟盘）。"""
    OKX_FOLLOW_USE_PAPER: bool = False

    OKX_FOLLOW_API_WHITELIST_IP: str = ""
    OKX_FOLLOW_API_LABEL: str = ""

    def is_configured(self) -> bool:
        return bool(
            self.OKX_FOLLOW_API_KEY
            and self.OKX_FOLLOW_SECRET_KEY
            and self.OKX_FOLLOW_PASSPHRASE
        )


follow_order_config = FollowOrderConfig()


def _sign(secret: str, ts: str, method: str, request_path: str, body: str) -> str:
    msg = ts + method.upper() + request_path + body
    mac = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def _json_ok(resp_status: int, data: dict[str, Any]) -> bool:
    c = data.get("code")
    return resp_status == 200 and (str(c) == "0" if c is not None else False)


class OkxFollowOrderClient:
    """
    跟单账户 OKX 客户端：下单、查持仓、追加逐仓保证金。
    须配置 OKX_FOLLOW_API_KEY / SECRET / PASSPHRASE。
    """

    def __init__(self, config: FollowOrderConfig | None = None) -> None:
        self._cfg = config or follow_order_config

    def is_configured(self) -> bool:
        return self._cfg.is_configured()

    def _headers(self, method: str, request_path: str, body: str) -> dict[str, str]:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        sig = _sign(self._cfg.OKX_FOLLOW_SECRET_KEY, ts, method, request_path, body)
        h: dict[str, str] = {
            "OK-ACCESS-KEY": self._cfg.OKX_FOLLOW_API_KEY,
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self._cfg.OKX_FOLLOW_PASSPHRASE,
            "Content-Type": "application/json",
        }
        if self._cfg.OKX_FOLLOW_USE_PAPER:
            h["x-simulated-trading"] = "1"
        return h

    def _not_configured_response(self) -> tuple[bool, dict[str, str]]:
        return False, {"msg": "OKX 跟单 API 未配置（需 OKX_FOLLOW_* 环境变量）"}

    def _build_body(self, params: dict[str, Any]) -> str:
        payload = {k: v for k, v in params.items() if v is not None}
        return json.dumps(payload, separators=(",", ":"))

    async def _parse_http_json(self, resp: aiohttp.ClientResponse) -> tuple[bool, Any]:
        text = await resp.text()
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError:
            return False, {"msg": "invalid json", "raw": text[:500]}
        if not isinstance(data, dict):
            return False, {"msg": "unexpected json shape", "raw": text[:200]}
        return _json_ok(resp.status, data), data

    async def _get(self, request_path: str) -> tuple[bool, Any]:
        """request_path 须含路径与 query，如 /api/v5/trade/fills?instType=SWAP&limit=20"""
        if not self._cfg.is_configured():
            return self._not_configured_response()

        url = _OKX_REST + request_path
        headers = self._headers("GET", request_path, "")

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                return await self._parse_http_json(resp)

    async def get_trade_fills(
        self,
        *,
        inst_type: str = "SWAP",
        inst_id: str | None = None,
        limit: int = 50,
    ) -> tuple[bool, Any]:
        """GET /api/v5/trade/fills — 最近成交（用于前端展示合约成交记录）。"""
        q: dict[str, str] = {
            "instType": inst_type,
            "limit": str(max(1, min(limit, 100))),
        }
        if inst_id:
            q["instId"] = inst_id.strip()
        path = "/api/v5/trade/fills?" + urlencode(q)
        return await self._get(path)

    async def get_margin_transfer_bills(
        self,
        *,
        inst_type: str = "SWAP",
        limit: int = 100,
    ) -> tuple[bool, Any]:
        """
        GET /api/v5/account/bills-archive
        type=6：保证金划转（含手动追加/减少逐仓保证金等，以交易所落账为准）。
        """
        q: dict[str, str] = {
            "instType": inst_type,
            "type": "6",
            "limit": str(max(1, min(limit, 100))),
        }
        path = "/api/v5/account/bills-archive?" + urlencode(q)
        return await self._get(path)

    async def get_positions_inst(self, inst_type: str = "SWAP") -> tuple[bool, Any]:
        """GET /api/v5/account/positions"""
        if not self._cfg.is_configured():
            return self._not_configured_response()

        path = f"/api/v5/account/positions?instType={inst_type}"
        url = _OKX_REST + path
        headers = self._headers("GET", path, "")

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                return await self._parse_http_json(resp)

    async def add_position_margin(self, inst_id: str, pos_side: str, amt: str) -> tuple[bool, Any]:
        """POST /api/v5/account/position/margin-balance，type=add 增加逐仓保证金。"""
        if not self._cfg.is_configured():
            return self._not_configured_response()

        body_obj: dict[str, Any] = {
            "instId": inst_id,
            "posSide": pos_side,
            "type": "add",
            "amt": amt,
        }
        body = json.dumps(body_obj, separators=(",", ":"))
        url = _OKX_REST + _MARGIN_BALANCE_PATH
        headers = self._headers("POST", _MARGIN_BALANCE_PATH, body)

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=body.encode("utf-8")) as resp:
                return await self._parse_http_json(resp)

    async def place_order(self, params: dict[str, Any]) -> tuple[bool, Any]:
        """POST /api/v5/trade/order"""
        if not self._cfg.is_configured():
            return self._not_configured_response()

        body = self._build_body(params)
        url = _OKX_REST + _PLACE_ORDER_PATH
        headers = self._headers("POST", _PLACE_ORDER_PATH, body)

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, data=body.encode("utf-8")) as resp:
                return await self._parse_http_json(resp)


_default_client = OkxFollowOrderClient()


async def get_positions_inst(inst_type: str = "SWAP") -> tuple[bool, Any]:
    """使用默认跟单凭证查询持仓（供 margin_monitor 等调用）。"""
    return await _default_client.get_positions_inst(inst_type)


async def add_position_margin(inst_id: str, pos_side: str, amt: str) -> tuple[bool, Any]:
    """使用默认跟单凭证追加逐仓保证金。"""
    return await _default_client.add_position_margin(inst_id, pos_side, amt)
