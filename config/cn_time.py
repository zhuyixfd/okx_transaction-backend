"""全项目统一使用北京时间（Asia/Shanghai）。"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

CN_TZ = ZoneInfo("Asia/Shanghai")


def now_cn() -> datetime:
    """当前北京时间（带 tzinfo）。"""
    return datetime.now(CN_TZ)


def as_beijing(dt: datetime | None) -> datetime | None:
    """
    转为北京时间，供 ORM 读出值参与序列化。

    - 已带时区：换算到 Asia/Shanghai。
    - naive：视为已是会话时区北京时间（与连接上 `SET time_zone = '+08:00'` 一致）。
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=CN_TZ)
    return dt.astimezone(CN_TZ)
