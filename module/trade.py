import re
import time
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
    _session: aiohttp.ClientSession | None = None

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
                "posCcy": pos["posCcy"],
                "posSide": pos["posSide"],
                "lever": pick_lever_from_pos(pos),
                "avgPx": pos.get("avgPx"),
                "last": pos.get("last"),
                "uplRatio": _fmt_upl_ratio_pct(pos),
                "upl": _fmt_upl_usdt(pos),
            })
        return res

    @classmethod
    async def close(cls) -> None:
        if cls._session and not cls._session.closed:
            await cls._session.close()
