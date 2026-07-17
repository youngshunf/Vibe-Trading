"""中国市场四工具的真实取数测试（A1 验收）—— 唤星 fork 新增（非上游代码）。

**真打 akshare，零 mock**（`integration` 标记，需要外网）。承担两件事：

1. **A1 验收**：「4 个 loader 与 finance 老结果逐接口对数一致」。搬运的本质是
   *同函数 + 同 kwargs + 同 akshare 版本*，故此处把老服务 ``interfaces.py`` 的
   映射表**原样复刻为期望值**，逐条断言新工具确实打到那个函数、那组 kwargs，
   并对同一时间窗做**逐字段值比对**——不是「看起来差不多」，是逐格相等。
2. **行序防呆**：断言各源的实际行序与代码里声明的 ``newest_first`` 一致。行序
   搞反不会报错，只会静默返回最古老的数据（2008 年的 CPI 冒充最新月份）。

跑法::

    .venv/bin/python -m pytest agent/tests/test_china_market_tools_live.py -m integration -q

上游端点抖动/限流时可能失败——那是真实信号，不要靠 mock 抹平。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from src.tools.china_market_tools import (
    ChinaFuturesTool,
    ChinaMacroTool,
    FundNavTool,
    FundPositionTool,
)

pytestmark = pytest.mark.integration

# 老服务 finance-data-service ``app/interfaces.py`` 的映射，原样复刻为 A1 期望值。
# 新工具必须打到**同一个 akshare 函数 + 同一组 kwargs**，否则 A1 不成立。
_LEGACY_INTERFACE_MAP = {
    "fund.nav_history": (
        "fund_open_fund_info_em",
        {"symbol": "000001", "indicator": "单位净值走势", "period": "成立来"},
    ),
    "futures.quote_history": ("futures_zh_daily_sina", {"symbol": "RB2510"}),
    "macro.indicator": ("macro_china_cpi", {}),
}


def _ak():
    """直接拿 akshare 模块，作为 A1 的「老路」参照。

    老服务的取数层就是 ``getattr(ak, spec.ak_func)(**kwargs)``——除了 asyncio
    包装（不改变返回值），与直调等价。故以直调 akshare 作为老结果基准。
    """
    import akshare as ak

    return ak


def _assert_rows_match_frame(rows: list[dict], frame: pd.DataFrame, *, newest_first: bool) -> None:
    """逐字段断言工具返回的行 == 老路 DataFrame 对应切片。

    Args:
        rows: 新工具信封里的 rows。
        frame: 老路直调 akshare 得到的 DataFrame。
        newest_first: 上游行序，决定比对头部还是尾部。
    """
    assert rows, "新工具返回空行，无法对数"
    expected = frame.head(len(rows)) if newest_first else frame.tail(len(rows))
    expected_records = expected.to_dict(orient="records")

    assert len(rows) == len(expected_records)
    for got, want in zip(rows, expected_records):
        for column, want_value in want.items():
            got_value = got[str(column)]
            if isinstance(want_value, float) and pd.isna(want_value):
                assert got_value is None, f"{column}: NaN 应归一为 None"
            elif isinstance(want_value, float):
                assert got_value == pytest.approx(want_value), f"{column} 值不一致"
            else:
                # 日期等非标量列在归一时转字符串，故按字符串比。
                assert str(got_value) == str(want_value), f"{column} 值不一致"


class TestA1MacroParity:
    """A1：中国宏观新旧两路逐字段一致 + 路由表随搬运生效。"""

    @pytest.mark.parametrize(
        ("indicator", "func_name"),
        [
            ("cpi", "macro_china_cpi"),
            ("ppi", "macro_china_ppi"),
            ("gdp", "macro_china_gdp"),
            ("pmi", "macro_china_pmi"),
        ],
    )
    def test_each_indicator_matches_legacy_akshare_call(
        self, indicator: str, func_name: str
    ) -> None:
        """同一 indicator、同一时间窗，新旧两路逐字段一致。"""
        out = json.loads(ChinaMacroTool().execute(indicator=indicator, max_rows=6))
        assert out["ok"] is True, out.get("error")
        assert out["data"]["akshare_function"] == func_name

        legacy_frame = getattr(_ak(), func_name)()

        assert out["data"]["total_rows"] == len(legacy_frame)
        _assert_rows_match_frame(out["data"]["rows"], legacy_frame, newest_first=True)

    def test_macro_series_is_actually_newest_first(self) -> None:
        """行序防呆：宏观确为新→旧，故声明的 newest_first=True 成立。

        若上游哪天改成旧→新，本测试会红——那正是该改代码的信号。
        """
        frame = _ak().macro_china_cpi()
        months = frame["月份"].tolist()

        assert months[0] > months[-1], "宏观序列不再是新→旧，frame_to_rows 的取头假设失效"

    def test_indicator_is_case_insensitive(self) -> None:
        """大小写/空格归一后仍能路由——分身传 ' CPI ' 不该失败。"""
        out = json.loads(ChinaMacroTool().execute(indicator="  CPI  ", max_rows=1))

        assert out["ok"] is True, out.get("error")
        assert out["data"]["akshare_function"] == "macro_china_cpi"


class TestA1FundNavParity:
    """A1：基金净值新旧两路逐字段一致。"""

    def test_nav_matches_legacy_akshare_call(self) -> None:
        func_name, kwargs = _LEGACY_INTERFACE_MAP["fund.nav_history"]
        out = json.loads(FundNavTool().execute(symbol=kwargs["symbol"], max_rows=5))
        assert out["ok"] is True, out.get("error")

        legacy_frame = getattr(_ak(), func_name)(**kwargs)

        assert out["data"]["total_rows"] == len(legacy_frame)
        _assert_rows_match_frame(out["data"]["rows"], legacy_frame, newest_first=False)

    def test_nav_defaults_match_legacy_spec(self) -> None:
        """默认 indicator/period 必须与老服务 spec 的默认值一致。"""
        _, kwargs = _LEGACY_INTERFACE_MAP["fund.nav_history"]
        props = FundNavTool.parameters["properties"]

        assert props["indicator"]["default"] == kwargs["indicator"]
        assert props["period"]["default"] == kwargs["period"]

    def test_nav_series_is_actually_oldest_first(self) -> None:
        """行序防呆：净值确为旧→新，故声明的 newest_first=False 成立。"""
        func_name, kwargs = _LEGACY_INTERFACE_MAP["fund.nav_history"]
        frame = getattr(_ak(), func_name)(**kwargs)
        dates = frame["净值日期"].tolist()

        assert str(dates[0]) < str(dates[-1]), "净值序列不再是旧→新，取尾假设失效"


class TestA1FuturesParity:
    """A1：国内期货新旧两路逐字段一致。"""

    def test_futures_matches_legacy_akshare_call(self) -> None:
        func_name, kwargs = _LEGACY_INTERFACE_MAP["futures.quote_history"]
        out = json.loads(ChinaFuturesTool().execute(symbol=kwargs["symbol"], max_rows=5))
        assert out["ok"] is True, out.get("error")

        legacy_frame = getattr(_ak(), func_name)(**kwargs)

        assert out["data"]["total_rows"] == len(legacy_frame)
        _assert_rows_match_frame(out["data"]["rows"], legacy_frame, newest_first=False)


class TestFundPositionViews:
    """基金持仓三视角的真实可用性——含个股级端点已下线的既成事实。"""

    def test_industry_view_returns_real_allocation(self) -> None:
        out = json.loads(
            FundPositionTool().execute(symbol="000001", view="industry", max_rows=5)
        )

        assert out["ok"] is True, out.get("error")
        assert out["data"]["akshare_function"] == "fund_portfolio_industry_allocation_em"
        assert out["data"]["rows"], "行业配置不应为空"
        assert "行业类别" in out["data"]["rows"][0]

    def test_asset_view_returns_real_asset_split(self) -> None:
        out = json.loads(FundPositionTool().execute(symbol="000001", view="asset"))

        assert out["ok"] is True, out.get("error")
        assert out["data"]["akshare_function"] == "fund_individual_detail_hold_xq"
        assert out["data"]["rows"], "大类资产配置不应为空"
        assert "资产类型" in out["data"]["rows"][0]

    def test_stock_view_failure_points_at_working_views(self) -> None:
        """个股级端点已被天天基金下线（2026-07-17 实测，老服务同样失败）。

        它可能哪天恢复——恢复了就该返回真实持仓；没恢复则错误必须**指路**可用
        视角，而不是让分身以为是自己参数写错了。两种情况都合格，故分叉断言。
        """
        out = json.loads(FundPositionTool().execute(symbol="000001", view="stock"))

        if out["ok"]:
            assert out["data"]["akshare_function"] == "fund_portfolio_hold_em"
            assert "股票代码" in out["data"]["rows"][0]
        else:
            assert "view='industry'" in out["error"]
            assert "view='asset'" in out["error"]
