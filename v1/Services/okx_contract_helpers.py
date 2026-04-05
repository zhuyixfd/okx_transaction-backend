"""
合约 U 本位永续：instId 规范化、账户 config 解析、逐仓可用性校验、杠杆读取。
供 manual_okx 路由与自动跟单真实下单共用。
"""

from __future__ import annotations

from typing import Any

# OKX 文档：逐仓 isolated 在跨币种(3)、组合保证金(4)账户下不可用。
ACCT_LV_NO_ISOLATED = frozenset({"3", "4"})


def normalize_swap_inst_id(raw: str) -> str:
    """统一为 U 本位永续 instId（*-USDT-SWAP）。仅「币-USDT」两段时补 -SWAP，完整 instId 保持原样。"""
    s = raw.strip().upper()
    if not s:
        return s
    if s.endswith("-SWAP"):
        return s
    if "-" not in s:
        return f"{s}-USDT-SWAP"
    parts = s.split("-")
    if len(parts) == 2:
        return f"{s}-SWAP"
    return s


def isolated_td_mode_blocked_reason(acct_lv: str | None) -> str | None:
    """
    acctLv 为 3（跨币种）或 4（组合保证金）时，OKX 不支持 U 本位逐仓。
    返回说明文案；None 表示可按逐仓下单。
    """
    if acct_lv in ACCT_LV_NO_ISOLATED:
        return (
            "当前欧易账户为跨币种或组合保证金模式（acctLv 3/4），不支持逐仓 U 本位合约。"
            "请在欧易将账户调整为单币种保证金模式后再使用。"
        )
    return None


def parse_account_config_fields(cfg_data: Any) -> tuple[str | None, str | None]:
    """返回 (acctLv, posMode)。posMode 为 net_mode 时下单/设杠杆不得传 long/short 的 posSide。"""
    if not isinstance(cfg_data, dict) or str(cfg_data.get("code")) != "0":
        return None, None
    rows = cfg_data.get("data")
    if not isinstance(rows, list) or not rows:
        return None, None
    first = rows[0]
    if not isinstance(first, dict):
        return None, None
    lv = first.get("acctLv")
    acct_lv = str(lv) if lv is not None and str(lv) != "" else None
    pm = first.get("posMode")
    pos_mode = str(pm).strip() if pm is not None and str(pm).strip() != "" else None
    return acct_lv, pos_mode


def sizing_lever_from_leverage_info(
    li_data: Any,
    *,
    hedge_mode: bool,
    pos_side: str,
) -> int | None:
    """从 leverage-info 响应中取杠杆（开平仓优先匹配 posSide）。"""
    if not isinstance(li_data, dict) or str(li_data.get("code")) != "0":
        return None
    rows = li_data.get("data")
    if not isinstance(rows, list) or not rows:
        return None

    def row_lever(r: object) -> int | None:
        if not isinstance(r, dict):
            return None
        v = r.get("lever")
        if v is None or str(v).strip() == "":
            return None
        try:
            x = int(float(str(v).strip()))
        except ValueError:
            return None
        return x if x >= 1 else None

    if hedge_mode:
        for r in rows:
            if isinstance(r, dict) and r.get("posSide") == pos_side:
                lv = row_lever(r)
                if lv is not None:
                    return lv
    for r in rows:
        if isinstance(r, dict) and r.get("posSide") == "net":
            lv = row_lever(r)
            if lv is not None:
                return lv
    for r in rows:
        lv = row_lever(r)
        if lv is not None:
            return lv
    return None
