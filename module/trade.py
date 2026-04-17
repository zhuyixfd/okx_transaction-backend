import re
import time
import json
from datetime import datetime

import aiohttp

from config.cn_time import CN_TZ


def pick_lever_from_pos(pos: dict) -> str:
    """社区接口杠杆字段名可能是 lever、leverage、posLever 等；统一成字符串。"""
    for key in (
        "lever",
        "leverage",
        "posLever",
        "leverRate",
        "leverMult",
        "leverageMultiple",
        "leverMultiple",
    ):
        v = pos.get(key)
        if v is None:
            continue
        s = str(v).strip()
        if s:
            return s
    return ""


def _fmt_upl_ratio_pct(pos: dict) -> str:
    v = pos.get("uplRatio")
    if v is None or (isinstance(v, str) and str(v).strip() == ""):
        return ""
    try:
        return str(round(float(v) * 100, 2))
    except (TypeError, ValueError):
        return str(v).strip()


def _fmt_upl_usdt(pos: dict) -> str:
    v = pos.get("upl")
    if v is None or (isinstance(v, str) and str(v).strip() == ""):
        return ""
    try:
        return f"{float(v):.3f}"
    except (TypeError, ValueError):
        return str(v).strip()


_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


class OkxTrade:
    query_position_current: str = (
        "https://www.oyigwcn.biz/priapi/v5/ecotrade/public/community/user/position-current"
    )
    query_position_history: str = (
        "https://www.oyigwcn.biz/priapi/v5/ecotrade/public/community/user/position-history"
    )
    _session: aiohttp.ClientSession | None = None
    overview_page_tpl: str = "https://www.oyigwcn.biz/zh-hans/copy-trading/account/{unique_name}?tab=trade"

    def __init__(self):
        pass

    @classmethod
    def get_session(cls) -> aiohttp.ClientSession:
        if cls._session is None or cls._session.closed:
            cls._session = aiohttp.ClientSession(headers=_DEFAULT_HEADERS)
        return cls._session

    @classmethod
    async def get_uniqueName(cls, url: str) -> tuple[str | None, str | None]:
        """
        从跟单落地页 HTML 中解析 nickName、uniqueName。

        链接示例: https://oyidl.net/ul/4DtIo37

        Returns:
            (nickname, unique_name)，任一可能为 None；解析失败时多为 (None, None)。
        """
        session = cls.get_session()
        async with session.get(url, allow_redirects=True) as response:
            html = await response.text()
        # JSON 片段中的字段，允许中间有空白
        nick_m = re.search(r'"nickName"\s*:\s*"(.*?)"', html)
        unique_m = re.search(r'"uniqueName"\s*:\s*"(.*?)"', html)
        nickname = nick_m.group(1) if nick_m else None
        unique_name = unique_m.group(1) if unique_m else None
        return (nickname, unique_name)

    @classmethod
    async def get_position_current(cls, uniqueName: str):
        """
        获取当前持仓。

        cTime / cTime_format
        pos: 持仓量
        margin: 保证金
        mgnRatio: 维持保证金率
        liqPx: 预估强平价
        posCcy: 币种
        posSide: long 做多，short 做空
        lever: 倍数
        avgPx: 开仓均价
        last: 标记价格
        uplRatio: 收益率
        upl: 盈亏
        """
        session = cls.get_session()
        async with session.get(
            url=cls.query_position_current,
            params={"uniqueName": uniqueName, "t": int(time.time() * 1000)},
        ) as response:
            data = await response.json()
            return await cls.clean_position_current(data)

    @classmethod
    async def get_position_current_safe(cls, uniqueName: str) -> tuple[bool, list[dict]]:
        """
        安全版当前持仓拉取。
        返回 (ok, positions):
        - ok=True  表示请求与返回结构正常（允许 positions 为空，代表真空仓）
        - ok=False 表示接口请求失败或返回结构异常（本轮应跳过，不做开平仓动作）
        """
        session = cls.get_session()
        try:
            async with session.get(
                url=cls.query_position_current,
                params={"uniqueName": uniqueName, "t": int(time.time() * 1000)},
            ) as response:
                if response.status != 200:
                    return (False, [])
                data = await response.json()
        except Exception:
            return (False, [])

        if not isinstance(data, dict):
            return (False, [])
        code = str(data.get("code", "")).strip()
        if code and code != "0":
            return (False, [])
        try:
            _ = data["data"][0]["posData"]
        except (KeyError, IndexError, TypeError):
            return (False, [])
        rows = await cls.clean_position_current(data)
        return (True, rows if isinstance(rows, list) else [])

    @classmethod
    async def get_position_history(cls, unique_name: str, limit: int = 100, offset: int = 0):
        """
        获取社区历史仓位（平仓记录）。
        说明：该接口返回结构在不同环境可能有差异，这里做宽松解析并统一输出列表。
        """
        session = cls.get_session()
        async with session.get(
            url=cls.query_position_history,
            params={
                "uniqueName": unique_name.strip(),
                "limit": int(limit),
                "offset": int(offset),
                "t": int(time.time() * 1000),
            },
        ) as response:
            data = await response.json()
            return cls.clean_position_history(data)

    @classmethod
    async def get_overview_data(cls, unique_name: str) -> dict:
        """
        从交易员公开页 HTML 中解析 overviewData。
        返回形如：
        {
            "ccy": "...",
            "equity": "...",
            ...
        }
        """
        url = cls.overview_page_tpl.format(unique_name=unique_name.strip())
        session = cls.get_session()
        async with session.get(url, allow_redirects=True) as response:
            html = await response.text()

        m = re.search(r'"overviewData"\s*:\s*\{', html)
        if not m:
            return {}
        start = html.find("{", m.start())
        if start < 0:
            return {}

        depth = 0
        end = -1
        in_str = False
        esc = False
        for i in range(start, len(html)):
            ch = html[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end <= start:
            return {}

        raw_obj = html[start:end + 1]
        try:
            d = json.loads(raw_obj)
        except Exception:
            return {}
        return d if isinstance(d, dict) else {}

    @staticmethod
    async def clean_position_current(data: dict):
        try:
            posData = data["data"][0]["posData"]
        except (KeyError, IndexError, TypeError):
            return []
        res: list = []
        if len(posData) == 0:
            return res
        for pos in posData:
            res.append({
                'posId': pos["posId"],
                "cTime": pos["cTime"],
                "cTime_format": datetime.fromtimestamp(
                    int(pos["cTime"]) / 1000,
                    tz=CN_TZ,
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "pos": pos.get("pos", ""),
                "mgnRatio": pos.get("mgnRatio", ""),
                "margin": pos.get("margin", ""),
                "liqPx": pos.get("liqPx", ""),
                "posCcy": pos["posCcy"],
                "posSide": pos["posSide"],
                "lever": pick_lever_from_pos(pos),
                "avgPx": pos.get("avgPx"),
                "last": pos.get("last"),
                "uplRatio": _fmt_upl_ratio_pct(pos),
                "upl": _fmt_upl_usdt(pos),
                "notionalUsd": pos.get("notionalUsd", pos.get("notional_usd", "")),
                "notionalCcy": pos.get("notionalCcy", pos.get("notional_ccy", "")),
                "notional": pos.get("notional", ""),
            })
        return res

    @staticmethod
    def clean_position_history(data: dict) -> list[dict]:
        if not isinstance(data, dict):
            return []
        raw = data.get("data")
        rows: list[dict] = []
        if isinstance(raw, list):
            rows = [r for r in raw if isinstance(r, dict)]
        elif isinstance(raw, dict):
            for key in ("items", "list", "history", "positionList", "data"):
                v = raw.get(key)
                if isinstance(v, list):
                    rows = [r for r in v if isinstance(r, dict)]
                    if rows:
                        break
        return rows

    @classmethod
    async def close(cls) -> None:
        if cls._session and not cls._session.closed:
            await cls._session.close()
