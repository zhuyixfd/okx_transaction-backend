"""
OKX v5 私有接口（下单 / 持仓 / 成交 / 保证金账单等）。

- 类 OkxFollowOrderClient：封装签名与 HTTP；密钥通过 OkxFollowRuntimeConfig / okx_client_for_db_secrets 注入。
- 路由层用数据库凭证时统一经 v1.Services.okx_account_client.require_okx_client。
- .env 仅可选：OKX_FOLLOW_REST_BASE、OKX_FOLLOW_USE_PAPER（模拟盘请求头）。

主要方法：place_order、place_swap_market_by_principal_usdt、get_positions_inst、get_trade_fills、
get_margin_transfer_bills、add_position_margin、get_account_config、set_leverage 等。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import aiohttp
from pydantic_settings import BaseSettings, SettingsConfigDict

# 与相对 CWD 的 ".env" 不同：始终读 backend/.env（避免从仓库根目录启动时读不到）
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _BACKEND_ROOT / ".env"

_PLACE_ORDER_PATH = "/api/v5/trade/order"
_CLOSE_POSITION_PATH = "/api/v5/trade/close-position"
_MARGIN_BALANCE_PATH = "/api/v5/account/position/margin-balance"
_SET_LEVERAGE_PATH = "/api/v5/account/set-leverage"
_SET_POSITION_MODE_PATH = "/api/v5/account/set-position-mode"

_DEFAULT_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=45, connect=15)


class OkxConnectionSettings(BaseSettings):
    """仅连接相关：REST 域名、是否走欧易模拟盘头；密钥从数据库注入 OkxFollowRuntimeConfig。"""

    model_config = SettingsConfigDict(
        env_file=_ENV_PATH,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    OKX_FOLLOW_USE_PAPER: bool = False
    OKX_FOLLOW_REST_BASE: str = "https://www.okx.com"


okx_connection_settings = OkxConnectionSettings()


class OkxFollowRuntimeConfig:
    """内存中的 OKX 凭证（供 OkxFollowOrderClient 使用）。"""

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        passphrase: str,
        *,
        use_paper: bool = False,
        rest_base: str = "https://www.okx.com",
    ) -> None:
        self.OKX_FOLLOW_API_KEY = api_key
        self.OKX_FOLLOW_SECRET_KEY = secret_key
        self.OKX_FOLLOW_PASSPHRASE = passphrase
        self.OKX_FOLLOW_USE_PAPER = use_paper
        self.OKX_FOLLOW_REST_BASE = (rest_base or "https://www.okx.com").strip().rstrip("/")

    def is_configured(self) -> bool:
        return bool(
            (self.OKX_FOLLOW_API_KEY or "").strip()
            and (self.OKX_FOLLOW_SECRET_KEY or "").strip()
            and (self.OKX_FOLLOW_PASSPHRASE or "").strip()
        )


def okx_client_for_db_secrets(api_key: str, api_secret: str, api_passphrase: str) -> OkxFollowOrderClient:
    """使用数据库中的密钥构造客户端（REST/模拟盘头仍读 .env OkxConnectionSettings）。"""
    return OkxFollowOrderClient(
        OkxFollowRuntimeConfig(
            (api_key or "").strip(),
            (api_secret or "").strip(),
            (api_passphrase or "").strip(),
            use_paper=okx_connection_settings.OKX_FOLLOW_USE_PAPER,
            rest_base=okx_connection_settings.OKX_FOLLOW_REST_BASE or "https://www.okx.com",
        )
    )


def _sign(secret: str, ts: str, method: str, request_path: str, body: str) -> str:
    msg = ts + method.upper() + request_path + body
    mac = hmac.new(secret.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(mac).decode("ascii")


def _json_ok(resp_status: int, data: dict[str, Any]) -> bool:
    c = data.get("code")
    return resp_status == 200 and (str(c) == "0" if c is not None else False)


class OkxFollowOrderClient:
    """
    OKX 私有接口客户端；须通过 OkxFollowRuntimeConfig 或 okx_client_for_db_secrets 注入密钥。
    """

    def __init__(self, config: OkxFollowRuntimeConfig | None = None) -> None:
        self._cfg = config or OkxFollowRuntimeConfig(
            "",
            "",
            "",
            use_paper=okx_connection_settings.OKX_FOLLOW_USE_PAPER,
            rest_base=okx_connection_settings.OKX_FOLLOW_REST_BASE or "https://www.okx.com",
        )

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
        return False, {"msg": "OKX API 未配置（请在系统中添加 okx_api_accounts 并传入 okx_api_account_id）"}

    def _rest_base(self) -> str:
        return (self._cfg.OKX_FOLLOW_REST_BASE or "https://www.okx.com").strip().rstrip("/")

    def _connect_error_payload(self, exc: BaseException) -> dict[str, str]:
        return {
            "msg": "无法连接 OKX（网络或 DNS 异常）",
            "detail": str(exc),
            "hint": (
                "若 detail 中出现 169.254.x.x，说明 www.okx.com 被错误解析，请检查本机 hosts、DNS、"
                "代理/VPN；可在服务器执行 nslookup www.okx.com 核对。也可在 .env 设置 OKX_FOLLOW_REST_BASE "
                "为当前环境可访问的 OKX API 域名（需与官方文档一致）。"
            ),
        }

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

        url = self._rest_base() + request_path
        headers = self._headers("GET", request_path, "")

        try:
            async with aiohttp.ClientSession(timeout=_DEFAULT_HTTP_TIMEOUT) as session:
                async with session.get(url, headers=headers) as resp:
                    return await self._parse_http_json(resp)
        except asyncio.TimeoutError:
            return False, {"msg": "连接 OKX 超时", "hint": "检查网络、防火墙或代理"}
        except aiohttp.ClientError as e:
            return False, self._connect_error_payload(e)

    async def _post(self, request_path: str, body: str) -> tuple[bool, Any]:
        if not self._cfg.is_configured():
            return self._not_configured_response()

        url = self._rest_base() + request_path
        headers = self._headers("POST", request_path, body)

        try:
            async with aiohttp.ClientSession(timeout=_DEFAULT_HTTP_TIMEOUT) as session:
                async with session.post(url, headers=headers, data=body.encode("utf-8")) as resp:
                    return await self._parse_http_json(resp)
        except asyncio.TimeoutError:
            return False, {"msg": "连接 OKX 超时", "hint": "检查网络、防火墙或代理"}
        except aiohttp.ClientError as e:
            return False, self._connect_error_payload(e)

    async def _get_public(self, request_path: str) -> tuple[bool, Any]:
        """公共接口，无需 API Key（用于按 USDT 本金换算张数）。"""
        url = self._rest_base() + request_path
        try:
            async with aiohttp.ClientSession(timeout=_DEFAULT_HTTP_TIMEOUT) as session:
                async with session.get(url) as resp:
                    return await self._parse_http_json(resp)
        except asyncio.TimeoutError:
            return False, {"msg": "连接 OKX 超时"}
        except aiohttp.ClientError as e:
            return False, self._connect_error_payload(e)

    def _fmt_okx_sz(self, d: Decimal) -> str:
        s = format(d.normalize(), "f")
        if "." in s:
            s = s.rstrip("0").rstrip(".")
        return s if s else "0"

    async def swap_sz_from_usdt_principal(
        self,
        inst_id: str,
        principal_usdt_str: str,
        *,
        leverage: int,
    ) -> tuple[bool, str | dict[str, Any]]:
        """
        U 本位 linear 永续：按保证金×杠杆得到目标名义后换算张数。
        """
        try:
            margin = Decimal(str(principal_usdt_str).strip())
        except InvalidOperation:
            return False, {"msg": "本金 USDT 格式无效"}
        if margin <= 0:
            return False, {"msg": "本金须大于 0"}
        if leverage < 1:
            return False, {"msg": "杠杆须为不小于 1 的整数"}
        notional = margin * Decimal(leverage)

        q_inst = urlencode({"instType": "SWAP", "instId": inst_id.strip().upper()})
        ok_i, raw_i = await self._get_public(f"/api/v5/public/instruments?{q_inst}")
        if not ok_i:
            return False, raw_i if isinstance(raw_i, dict) else {"msg": str(raw_i)}
        rows = raw_i.get("data") if isinstance(raw_i, dict) else None
        if not isinstance(rows, list) or not rows:
            return False, {"msg": "未找到合约信息", "instId": inst_id}
        inst = rows[0]
        if not isinstance(inst, dict):
            return False, {"msg": "合约数据异常"}
        ct_type = str(inst.get("ctType") or "").lower()
        if ct_type != "linear":
            return False, {
                "msg": "当前仅支持 U 本位（linear）永续按本金下单",
                "ctType": ct_type or "unknown",
            }
        try:
            ct_val = Decimal(str(inst.get("ctVal") or "0"))
            lot_sz = Decimal(str(inst.get("lotSz") or "1"))
            min_sz = Decimal(str(inst.get("minSz") or "1"))
        except InvalidOperation:
            return False, {"msg": "合约 lotSz/ctVal 解析失败"}
        if ct_val <= 0 or lot_sz <= 0:
            return False, {"msg": "合约 ctVal/lotSz 无效"}

        q_t = urlencode({"instId": inst_id.strip().upper()})
        ok_t, raw_t = await self._get_public(f"/api/v5/market/ticker?{q_t}")
        if not ok_t:
            return False, raw_t if isinstance(raw_t, dict) else {"msg": str(raw_t)}
        trows = raw_t.get("data") if isinstance(raw_t, dict) else None
        if not isinstance(trows, list) or not trows:
            return False, {"msg": "未取到行情", "instId": inst_id}
        tick = trows[0]
        if not isinstance(tick, dict):
            return False, {"msg": "行情数据异常"}
        px_s = tick.get("markPx") or tick.get("last") or tick.get("idxPx") or ""
        try:
            px = Decimal(str(px_s).strip())
        except InvalidOperation:
            return False, {"msg": "标记价/最新价无效", "raw": str(px_s)[:32]}
        if px <= 0:
            return False, {"msg": "价格无效"}

        denom = ct_val * px
        sz_raw = notional / denom
        steps = (sz_raw / lot_sz).to_integral_value(rounding=ROUND_DOWN)
        sz_adj = steps * lot_sz
        if sz_adj <= 0 or sz_adj < min_sz:
            return False, {
                "msg": "按当前价计算张数低于最小下单量，请提高本金或杠杆",
                "minSz": str(min_sz),
                "lotSz": str(lot_sz),
                "computedSz": self._fmt_okx_sz(sz_adj),
                "markPx": str(px),
            }
        return True, self._fmt_okx_sz(sz_adj)

    async def place_swap_market_by_principal_usdt(
        self,
        inst_id: str,
        principal_usdt: str,
        *,
        leverage: int,
        td_mode: str,
        side: str,
        pos_side: str | None = None,
    ) -> tuple[bool, Any]:
        """
        U 本位永续市价开仓：principal_usdt 为保证金(USDT)，内部换算张数后下单。
        成功: (True, OKX 下单响应体)
        失败: (False, ("sz", detail)) 换算失败；(False, ("place", detail)) 下单失败
        """
        ok_sz, sz_or_err = await self.swap_sz_from_usdt_principal(
            inst_id,
            principal_usdt.strip(),
            leverage=leverage,
        )
        if not ok_sz:
            return False, ("sz", sz_or_err)

        iid = inst_id.strip().upper()
        params: dict[str, Any] = {
            "instId": iid,
            "tdMode": td_mode,
            "side": side,
            "ordType": "market",
            "sz": str(sz_or_err),
        }
        if pos_side:
            params["posSide"] = pos_side
        if "-SWAP" in iid and td_mode == "isolated":
            parts = iid.split("-")
            params["ccy"] = parts[1] if len(parts) >= 2 and parts[1] else "USDT"

        ok_po, data = await self.place_order(params)
        if not ok_po:
            return False, ("place", data)
        return True, data

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
        path = f"/api/v5/account/positions?instType={inst_type}"
        return await self._get(path)

    async def get_account_balance(self, ccy: str = "USDT") -> tuple[bool, Any]:
        """GET /api/v5/account/balance"""
        q = urlencode({"ccy": ccy.strip().upper()}) if ccy else ""
        path = "/api/v5/account/balance" + (f"?{q}" if q else "")
        return await self._get(path)

    async def get_positions_history(
        self,
        inst_type: str = "SWAP",
        mgn_mode: str | None = None,
        limit: int = 100,
    ) -> tuple[bool, Any]:
        """GET /api/v5/account/positions-history"""
        q: dict[str, str] = {
            "instType": inst_type.strip().upper(),
            "limit": str(max(1, min(limit, 100))),
        }
        if mgn_mode and str(mgn_mode).strip():
            q["mgnMode"] = str(mgn_mode).strip().lower()
        path = "/api/v5/account/positions-history?" + urlencode(q)
        return await self._get(path)

    async def get_account_config(self) -> tuple[bool, Any]:
        """GET /api/v5/account/config（含 acctLv、posMode 等）。"""
        return await self._get("/api/v5/account/config")

    async def get_leverage_info(self, inst_id: str, mgn_mode: str) -> tuple[bool, Any]:
        """GET /api/v5/account/leverage-info"""
        q = urlencode({"instId": inst_id.strip().upper(), "mgnMode": mgn_mode.strip()})
        return await self._get(f"/api/v5/account/leverage-info?{q}")

    async def add_position_margin(self, inst_id: str, pos_side: str, amt: str) -> tuple[bool, Any]:
        """POST /api/v5/account/position/margin-balance，type=add 增加逐仓保证金。"""
        iid = inst_id.strip().upper()
        body_obj: dict[str, Any] = {
            "instId": iid,
            "posSide": str(pos_side).strip().lower(),
            "type": "add",
            "amt": str(amt).strip(),
        }
        # U 本位永续与下单一致：逐仓需带保证金币种，否则部分帐户追加会失败
        if "-SWAP" in iid:
            parts = iid.split("-")
            if len(parts) >= 2 and parts[1]:
                body_obj["ccy"] = parts[1]
        body = json.dumps(body_obj, separators=(",", ":"))
        return await self._post(_MARGIN_BALANCE_PATH, body)

    async def set_leverage(
        self,
        inst_id: str,
        lever: str,
        mgn_mode: str,
        *,
        pos_side: str | None = None,
        ccy: str | None = None,
    ) -> tuple[bool, Any]:
        """
        POST /api/v5/account/set-leverage。
        逐仓 + 开平仓模式下 pos_side 传 long/short；单向 net 一般不传 posSide。
        """
        obj: dict[str, Any] = {
            "instId": inst_id,
            "lever": lever.strip(),
            "mgnMode": mgn_mode,
        }
        if pos_side:
            obj["posSide"] = pos_side
        if ccy:
            obj["ccy"] = ccy.strip()
        body = json.dumps(obj, separators=(",", ":"))
        return await self._post(_SET_LEVERAGE_PATH, body)

    async def set_position_mode(self, pos_mode: str) -> tuple[bool, Any]:
        """POST /api/v5/account/set-position-mode（long_short_mode=开平仓/双向）。"""
        body = json.dumps({"posMode": pos_mode.strip()}, separators=(",", ":"))
        return await self._post(_SET_POSITION_MODE_PATH, body)

    async def place_order(self, params: dict[str, Any]) -> tuple[bool, Any]:
        """POST /api/v5/trade/order"""
        body = self._build_body(params)
        return await self._post(_PLACE_ORDER_PATH, body)

    async def close_swap_position(
        self,
        inst_id: str,
        mgn_mode: str,
        pos_side: str | None,
    ) -> tuple[bool, Any]:
        """
        POST /api/v5/trade/close-position：市价平掉指定合约方向持仓。
        net 模式：pos_side 传 None（由 OKX 默认 net）；long/short 模式须传 long 或 short。
        """
        iid = inst_id.strip().upper()
        obj: dict[str, Any] = {"instId": iid, "mgnMode": mgn_mode.strip()}
        if pos_side:
            obj["posSide"] = str(pos_side).strip().lower()
        body = json.dumps(obj, separators=(",", ":"))
        return await self._post(_CLOSE_POSITION_PATH, body)


async def get_positions_inst(
    inst_type: str = "SWAP",
    client: OkxFollowOrderClient | None = None,
) -> tuple[bool, Any]:
    """查询持仓；须传入已配置密钥的 client。"""
    c = client if client is not None else OkxFollowOrderClient()
    return await c.get_positions_inst(inst_type)


async def add_position_margin(
    inst_id: str,
    pos_side: str,
    amt: str,
    client: OkxFollowOrderClient | None = None,
) -> tuple[bool, Any]:
    """追加逐仓保证金；须传入已配置密钥的 client。"""
    c = client if client is not None else OkxFollowOrderClient()
    return await c.add_position_margin(inst_id, pos_side, amt)
