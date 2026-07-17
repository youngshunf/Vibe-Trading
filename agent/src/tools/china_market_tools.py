"""中国市场四类只读数据工具 —— 唤星 fork 新增（非上游代码）。

从 ``finance-data-service`` 原样搬运其**已验证的 akshare 调用**（同函数、同 kwargs、
同 akshare 1.18.64），补齐上游引擎的四处能力欠账：

| 工具 | 补的欠账 | akshare 函数 |
| --- | --- | --- |
| ``get_china_macro`` | 上游 ``get_macro_series`` 走 FredMacroTool，是**美国宏观** | ``macro_china_{cpi,ppi,gdp,pmi}``（按 indicator 路由） |
| ``get_fund_nav`` | 上游 ``get_fund_flow`` 是资金流向 ≠ 基金净值 | ``fund_open_fund_info_em`` |
| ``get_fund_position`` | 上游无 | 按 ``view`` 路由，见下 |
| ``get_futures_daily`` | 上游 ``china_futures.py`` 是回测引擎，不是数据源 | ``futures_zh_daily_sina`` |

四者都是严格只读的行情/统计数据，不下单、不碰实盘端点——与本目录其余数据类
工具（``get_northbound_flow`` / ``get_margin_trading`` 等）同级。

工具描述（``description`` / ``parameters``）保持英文：它们是喂给 LLM 的提示词
文本，须与引擎既有工具面语言一致；说明性注释与 docstring 用中文。
"""

from __future__ import annotations

import logging
from typing import Any

from src.agent.tools import BaseTool
from src.tools.akshare_bridge import (
    MACRO_ROUTES,
    AkshareCallError,
    call_akshare,
    clamp_max_rows,
    error_envelope,
    frame_to_rows,
    ok_envelope,
)

logger = logging.getLogger(__name__)

# 四个工具共用的 max_rows 参数声明，避免四份重复。
_MAX_ROWS_PARAM: dict[str, Any] = {
    "type": "integer",
    "description": (
        "Maximum number of most-recent rows to return (older rows are dropped). "
        "Clamped to 1..500; defaults to 120."
    ),
    "default": 120,
}


def _truncation_warnings(returned: int, total: int) -> list[str]:
    """结果被截断时生成告警文案。

    诚实透出「你只看到了一部分」，避免分身把截断后的尾部当成全量做判断。

    Args:
        returned: 实际返回的行数。
        total: 截断前的总行数。

    Returns:
        告警列表；未截断时为空。
    """
    if total > returned:
        return [
            f"结果按最近 {returned} 行截断（上游共 {total} 行）；"
            "如需更长历史，调大 max_rows。"
        ]
    return []


class ChinaMacroTool(BaseTool):
    """中国宏观指标（CPI / PPI / GDP / PMI）。"""

    name = "get_china_macro"
    description = (
        "Fetch CHINA macroeconomic indicator time series: CPI (居民消费价格指数), "
        "PPI (工业生产者出厂价格指数), GDP (国内生产总值) or PMI (采购经理人指数), "
        "published by China's National Bureau of Statistics via AKShare. "
        "Use this for the Chinese economy — NOT get_macro_series, which serves "
        "US FRED series (CPIAUCSL / UNRATE / GDPC1 / FEDFUNDS). Read-only, no "
        "credentials. Example: get_china_macro(indicator='cpi', max_rows=24)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "indicator": {
                "type": "string",
                "enum": sorted(MACRO_ROUTES),
                "description": (
                    "Which China macro series to fetch: 'cpi' (consumer price "
                    "index, monthly), 'ppi' (producer price index, monthly), "
                    "'gdp' (gross domestic product, quarterly) or 'pmi' "
                    "(purchasing managers' index, monthly)."
                ),
                "default": "cpi",
            },
            "max_rows": _MAX_ROWS_PARAM,
        },
        "required": [],
    }

    def execute(self, **kwargs: Any) -> str:
        """取一条中国宏观指标序列。

        Args:
            **kwargs: ``indicator``（默认 ``cpi``）与 ``max_rows``。

        Returns:
            JSON 信封字符串。
        """
        indicator = str(kwargs.get("indicator", "cpi")).lower().strip()
        max_rows = clamp_max_rows(kwargs.get("max_rows", 120))

        # 按 indicator 路由到具体 akshare 函数——这是原服务 __macro_router__ 特例的搬运。
        func_name = MACRO_ROUTES.get(indicator)
        if func_name is None:
            return error_envelope(
                AkshareCallError(
                    f"未知宏观指标 '{indicator}'（支持：{', '.join(sorted(MACRO_ROUTES))}）",
                    kind="bad_request",
                )
            )

        try:
            frame = call_akshare(func_name, {})
        except AkshareCallError as exc:
            logger.warning("中国宏观 %s 取数失败：%s", indicator, exc)
            return error_envelope(exc)

        # 实测：macro_china_* 四个函数均为新→旧（首行是最近月份/季度）。
        rows, total = frame_to_rows(frame, max_rows, newest_first=True)
        return ok_envelope(
            market="China macro",
            data={
                "indicator": indicator,
                "akshare_function": func_name,
                "order": "newest_first",
                "total_rows": total,
                "returned_rows": len(rows),
                "rows": rows,
            },
            warnings=_truncation_warnings(len(rows), total),
        )


class FundNavTool(BaseTool):
    """公募基金历史净值。"""

    name = "get_fund_nav"
    description = (
        "Fetch a CHINA open-end mutual fund's historical net asset value (净值) "
        "series from Eastmoney via AKShare: unit NAV, cumulative NAV, or "
        "cumulative return, one row per disclosure date. This is fund NAV — NOT "
        "get_fund_flow, which returns per-stock order-bucket capital inflow. "
        "Read-only, no credentials, China funds only. "
        "Example: get_fund_nav(symbol='000001', max_rows=60)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": (
                    "Six-digit China fund code, no exchange suffix, e.g. '000001' "
                    "or '110022'."
                ),
            },
            "indicator": {
                "type": "string",
                "description": (
                    "Which NAV series to fetch. Defaults to '单位净值走势' (unit "
                    "NAV). Other common values: '累计净值走势' (cumulative NAV), "
                    "'累计收益率走势' (cumulative return)."
                ),
                "default": "单位净值走势",
            },
            "period": {
                "type": "string",
                "description": (
                    "History window. Defaults to '成立来' (since inception). "
                    "Only applies to the cumulative-return indicator."
                ),
                "default": "成立来",
            },
            "max_rows": _MAX_ROWS_PARAM,
        },
        "required": ["symbol"],
    }

    def execute(self, **kwargs: Any) -> str:
        """取一只基金的历史净值。

        Args:
            **kwargs: ``symbol``（必填）、``indicator``、``period``、``max_rows``。

        Returns:
            JSON 信封字符串。
        """
        symbol = str(kwargs.get("symbol", "")).strip()
        if not symbol:
            return error_envelope(
                AkshareCallError("缺少必填参数 symbol（六位基金代码）", kind="bad_request")
            )
        indicator = str(kwargs.get("indicator", "单位净值走势"))
        period = str(kwargs.get("period", "成立来"))
        max_rows = clamp_max_rows(kwargs.get("max_rows", 120))

        try:
            frame = call_akshare(
                "fund_open_fund_info_em",
                {"symbol": symbol, "indicator": indicator, "period": period},
            )
        except AkshareCallError as exc:
            logger.warning("基金净值 %s 取数失败：%s", symbol, exc)
            return error_envelope(exc)

        # 实测：净值序列为旧→新（首行是成立日），故取尾部才是最近净值。
        rows, total = frame_to_rows(frame, max_rows, newest_first=False)
        return ok_envelope(
            market="China fund",
            data={
                "symbol": symbol,
                "indicator": indicator,
                "order": "oldest_first",
                "total_rows": total,
                "returned_rows": len(rows),
                "rows": rows,
            },
            warnings=_truncation_warnings(len(rows), total),
        )


class FundPositionTool(BaseTool):
    """公募基金披露的持仓，按 ``view`` 分三种真实粒度。

    ⚠️ **为什么是三个 view 而不是一个**（2026-07-17 实测，勿轻易改回）：
    原 finance-data-service 的 ``fund.position`` 只映射 ``fund_portfolio_hold_em``
    （个股级重仓股）。实测该函数在 akshare 1.18.64 下对**任意基金、任意年份全部
    失败**——天天基金已下线其依赖的 ``fundf10.eastmoney.com/FundArchivesDatas.aspx``
    端点，改返 404 HTML；akshare 的 demjson 解析器撞上 HTML 里 CSS 的 ``{``，
    报出误导性的 ``Can not decode value starting with character ';'``。
    老服务的 venv 里同样复现 → 是**上游端点下线**，不是本 fork 的回归。

    同族的 ``fund_portfolio_change_em`` / ``fund_portfolio_bond_hold_em`` 走同一个
    死端点，一并不可用；而 ``fund_portfolio_industry_allocation_em``（行业配置）走
    **另一个仍存活的** API（``api.fund.eastmoney.com/f10/HYPZ/``），
    ``fund_individual_detail_hold_xq``（大类资产配置）走蛋卷 API，两者均实测可用。

    因此保留 ``stock`` 为默认（忠实原契约，端点若恢复即自动可用），并把两个可用
    粒度作为**显式 view** 暴露——不做静默替代：粒度不同的数据冒充个股持仓，比诚实
    报错更危险。``stock`` 失败时错误信封会指明可改用哪个 view。
    """

    name = "get_fund_position"
    description = (
        "Fetch a CHINA mutual fund's disclosed portfolio holdings (基金持仓) at one "
        "of three real granularities, selected by `view`: 'stock' (per-holding "
        "stock code/name/shares/market value/share of net assets), 'industry' "
        "(per-industry allocation and share of net assets) or 'asset' (top-level "
        "asset-class split across equity/bond/cash/other). Read-only, no "
        "credentials, China funds only. NOTE: the upstream data source behind "
        "view='stock' is currently returning errors for all funds; if it fails, "
        "retry with view='industry' or view='asset' — the error envelope says so "
        "explicitly. Example: get_fund_position(symbol='000001', view='industry')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": (
                    "Six-digit China fund code, no exchange suffix, e.g. '000001'."
                ),
            },
            "view": {
                "type": "string",
                "enum": ["stock", "industry", "asset"],
                "description": (
                    "Holding granularity: 'stock' = individual stock holdings, "
                    "'industry' = industry allocation, 'asset' = asset-class split."
                ),
                "default": "stock",
            },
            "date": {
                "type": "string",
                "description": (
                    "Reporting year as a four-digit string, e.g. '2024'. Applies to "
                    "view='stock' and view='industry'; ignored for view='asset', "
                    "which is a current snapshot."
                ),
                "default": "2024",
            },
            "max_rows": _MAX_ROWS_PARAM,
        },
        "required": ["symbol"],
    }

    def execute(self, **kwargs: Any) -> str:
        """取一只基金的持仓（粒度由 ``view`` 决定）。

        Args:
            **kwargs: ``symbol``（必填）、``view``、``date``、``max_rows``。

        Returns:
            JSON 信封字符串。
        """
        symbol = str(kwargs.get("symbol", "")).strip()
        if not symbol:
            return error_envelope(
                AkshareCallError("缺少必填参数 symbol（六位基金代码）", kind="bad_request")
            )
        view = str(kwargs.get("view", "stock")).lower().strip()
        date = str(kwargs.get("date", "2024")).strip()
        max_rows = clamp_max_rows(kwargs.get("max_rows", 120))

        if view == "stock":
            func_name = "fund_portfolio_hold_em"
            call_kwargs: dict[str, Any] = {"symbol": symbol, "date": date}
            newest_first = True
        elif view == "industry":
            func_name = "fund_portfolio_industry_allocation_em"
            call_kwargs = {"symbol": symbol, "date": date}
            newest_first = True
        elif view == "asset":
            func_name = "fund_individual_detail_hold_xq"
            call_kwargs = {"symbol": symbol}
            newest_first = False
        else:
            return error_envelope(
                AkshareCallError(
                    f"未知持仓视角 '{view}'（支持：stock / industry / asset）",
                    kind="bad_request",
                )
            )

        try:
            frame = call_akshare(func_name, call_kwargs)
        except AkshareCallError as exc:
            logger.warning("基金持仓 %s(%s/%s) 取数失败：%s", symbol, view, date, exc)
            if view == "stock":
                # 个股级端点已被上游下线：诚实告知并指路可用视角，别让分身以为是自己参数错了。
                return error_envelope(
                    AkshareCallError(
                        f"{exc}（个股级持仓的上游端点当前不可用；"
                        f"可改用 view='industry' 取行业配置，或 view='asset' 取大类资产配置）",
                        kind=exc.kind,
                    )
                )
            return error_envelope(exc)

        rows, total = frame_to_rows(frame, max_rows, newest_first=newest_first)
        return ok_envelope(
            market="China fund",
            data={
                "symbol": symbol,
                "view": view,
                "date": date,
                "akshare_function": func_name,
                "order": "newest_first" if newest_first else "oldest_first",
                "total_rows": total,
                "returned_rows": len(rows),
                "rows": rows,
            },
            warnings=_truncation_warnings(len(rows), total),
        )


class ChinaFuturesTool(BaseTool):
    """国内期货合约日线。"""

    name = "get_futures_daily"
    description = (
        "Fetch a CHINA domestic futures contract's daily OHLCV history from Sina "
        "via AKShare (SHFE / DCE / CZCE / INE). Covers commodity contracts such "
        "as 'RB2510' (rebar) or 'CU2512' (copper). Read-only, no credentials; "
        "China futures only — for equities use get_market_data. "
        "Example: get_futures_daily(symbol='RB2510', max_rows=60)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": (
                    "Futures contract code as listed on Sina, e.g. 'RB2510' "
                    "(rebar Oct-2025), 'CU2512' (copper), 'M2509' (soybean meal)."
                ),
            },
            "max_rows": _MAX_ROWS_PARAM,
        },
        "required": ["symbol"],
    }

    def execute(self, **kwargs: Any) -> str:
        """取一个国内期货合约的日线。

        Args:
            **kwargs: ``symbol``（必填）、``max_rows``。

        Returns:
            JSON 信封字符串。
        """
        symbol = str(kwargs.get("symbol", "")).strip()
        if not symbol:
            return error_envelope(
                AkshareCallError("缺少必填参数 symbol（期货合约代码）", kind="bad_request")
            )
        max_rows = clamp_max_rows(kwargs.get("max_rows", 120))

        try:
            frame = call_akshare("futures_zh_daily_sina", {"symbol": symbol})
        except AkshareCallError as exc:
            logger.warning("国内期货 %s 取数失败：%s", symbol, exc)
            return error_envelope(exc)

        # 实测：新浪期货日线为旧→新，取尾部得最近交易日。
        rows, total = frame_to_rows(frame, max_rows, newest_first=False)
        return ok_envelope(
            market="China futures",
            data={
                "symbol": symbol,
                "order": "oldest_first",
                "total_rows": total,
                "returned_rows": len(rows),
                "rows": rows,
            },
            warnings=_truncation_warnings(len(rows), total),
        )
