import re
import time
from datetime import datetime

import aiohttp


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
            cls._session = aiohttp.ClientSession()
        return cls._session

    @classmethod
    async def get_uniqueName(cls, url: str) -> str | None:
        """
        从落地页 URL 解析用户 uniqueName。

        url 示例: https://www.oyidl.net/ul/4DtIo37
        """
        session = cls.get_session()
        async with session.get(url) as response:
            html = await response.text()
            uniqueName_group = re.search('"uniqueName":.*?"(.*?)"', html)
            nickName_group = re.search('"nickName":.*?"(.*?)"', html)
            if not uniqueName_group:
                return None
            uniqueName = uniqueName_group.group(1)
            nickName = nickName_group.group(1)
            return (nickName, uniqueName)
        return None

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
        posData = data["data"][0]["posData"]
        res: dict = {}
        if len(posData) == 0:
            return res
        for pos in posData:
            res[pos["posId"]] = {
                "cTime": pos["cTime"],
                "cTime_format": datetime.fromtimestamp(
                    int(pos["cTime"]) / 1000
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "posCcy": pos["posCcy"],
                "posSide": pos["posSide"],
                "lever": pos["lever"],
                "avgPx": pos["avgPx"],
                "last": pos["last"],
            }
        return res

    @classmethod
    async def close(cls) -> None:
        if cls._session and not cls._session.closed:
            await cls._session.close()
