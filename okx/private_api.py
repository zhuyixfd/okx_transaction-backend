"""OKX REST 私有接口（v5）：签名请求，查询持仓、追加逐仓保证金。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from typing import Any

import aiohttp

from config.okx_private import okx_private_config

_OKX_REST = "https://www.okx.com"


def _sign(secret: str, ts: str, method: str, request_path: str, body: str) -> str:
    msg = ts + method.upper() + request_path + body
    mac = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def _headers(method: str, request_path: str, body: str) -> dict[str, str]:
    cfg = okx_private_config
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    sig = _sign(cfg.OKX_SECRET_KEY, ts, method, request_path, body)
    h: dict[str, str] = {
        "OK-ACCESS-KEY": cfg.OKX_API_KEY,
        "OK-ACCESS-SIGN": sig,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": cfg.OKX_PASSPHRASE,
        "Content-Type": "application/json",
    }
    if cfg.OKX_USE_PAPER:
        h["x-simulated-trading"] = "1"
    return h


async def get_positions_inst(inst_type: str = "SWAP") -> tuple[bool, Any]:
    """GET /api/v5/account/positions"""
    if not okx_private_config.is_configured():
        return False, {"msg": "OKX API 未配置"}

    path = f"/api/v5/account/positions?instType={inst_type}"
    url = _OKX_REST + path
    headers = _headers("GET", path, "")

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                return False, {"msg": "invalid json", "raw": text[:200]}
            c = data.get("code")
            ok = resp.status == 200 and (str(c) == "0" if c is not None else False)
            return ok, data


async def add_position_margin(inst_id: str, pos_side: str, amt: str) -> tuple[bool, Any]:
    """POST /api/v5/account/position/margin-balance（逐仓增加保证金）。"""
    if not okx_private_config.is_configured():
        return False, {"msg": "OKX API 未配置"}

    path = "/api/v5/account/position/margin-balance"
    body_obj: dict[str, Any] = {
        "instId": inst_id,
        "posSide": pos_side,
        "type": "add",
        "amt": amt,
    }
    body = json.dumps(body_obj, separators=(",", ":"))
    url = _OKX_REST + path
    headers = _headers("POST", path, body)

    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, data=body.encode("utf-8")) as resp:
            text = await resp.text()
            try:
                data = json.loads(text) if text else {}
            except json.JSONDecodeError:
                return False, {"msg": "invalid json", "raw": text[:200]}
            c = data.get("code")
            ok = resp.status == 200 and (str(c) == "0" if c is not None else False)
            return ok, data
