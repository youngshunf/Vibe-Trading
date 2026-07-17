"""中国市场四工具的单元测试 —— 唤星 fork 新增（非上游代码）。

只测**纯逻辑**：行序截取、行数夹取、宏观路由表、单元格归一、入参校验短路。
这些路径不触网，故用真实 DataFrame / 真实代码路径即可覆盖，无需任何 mock
（遵守「零 Mock 零 Fake」铁律）。真实取数的对数验收在
``test_china_market_tools_live.py``（``integration`` 标记）。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from src.tools.akshare_bridge import (
    MACRO_ROUTES,
    AkshareCallError,
    clamp_max_rows,
    error_envelope,
    frame_to_rows,
    ok_envelope,
)
from src.tools.china_market_tools import (
    ChinaFuturesTool,
    ChinaMacroTool,
    FundNavTool,
    FundPositionTool,
)

pytestmark = pytest.mark.unit


def _series_frame() -> pd.DataFrame:
    """构造一个 5 行的时间序列表，日期列可区分首尾。"""
    return pd.DataFrame(
        {
            "日期": ["d1", "d2", "d3", "d4", "d5"],
            "值": [1.0, 2.0, 3.0, 4.0, 5.0],
        }
    )


class TestFrameToRows:
    """行序截取——这是最容易搞反、且反了会静默返回最古老数据的地方。"""

    def test_newest_first_takes_head(self) -> None:
        """上游为新→旧时取头部，才是「最近 N 行」。"""
        rows, total = frame_to_rows(_series_frame(), 2, newest_first=True)

        assert total == 5
        assert [r["日期"] for r in rows] == ["d1", "d2"]

    def test_oldest_first_takes_tail(self) -> None:
        """上游为旧→新时取尾部，才是「最近 N 行」。

        回归钉子：曾一律 ``tail()``，导致 ``macro_china_cpi``（新→旧）返回
        2008 年的 CPI 冒充最新月份。
        """
        rows, total = frame_to_rows(_series_frame(), 2, newest_first=False)

        assert total == 5
        assert [r["日期"] for r in rows] == ["d4", "d5"]

    def test_total_reports_pre_truncation_count(self) -> None:
        """total 必须是截断前的总行数，否则调用方看不出被截断。"""
        rows, total = frame_to_rows(_series_frame(), 2, newest_first=False)

        assert total == 5
        assert len(rows) == 2

    def test_empty_and_none_frames(self) -> None:
        """空表与 None 都归一为空结果，不抛异常。"""
        assert frame_to_rows(None, 10, newest_first=True) == ([], 0)
        assert frame_to_rows(pd.DataFrame(), 10, newest_first=True) == ([], 0)

    def test_max_rows_larger_than_frame_returns_all(self) -> None:
        rows, total = frame_to_rows(_series_frame(), 100, newest_first=False)

        assert total == 5
        assert len(rows) == 5

    def test_nan_cells_become_none_so_json_stays_valid(self) -> None:
        """NaN 不能进 JSON——必须归一为 None，否则 json.dumps 出非法字面量。"""
        frame = pd.DataFrame({"值": [1.0, float("nan")]})

        rows, _ = frame_to_rows(frame, 10, newest_first=False)

        assert rows[1]["值"] is None
        json.dumps(rows)  # 不抛即合法

    def test_numpy_scalars_are_unwrapped_to_python(self) -> None:
        """numpy 标量须还原成原生 Python 值，否则 json 序列化失败。"""
        frame = pd.DataFrame({"整数": [1, 2], "浮点": [1.5, 2.5]})

        rows, _ = frame_to_rows(frame, 10, newest_first=False)

        assert isinstance(rows[0]["整数"], int)
        assert isinstance(rows[0]["浮点"], float)
        json.dumps(rows)


class TestClampMaxRows:
    """行数夹取——防分身传 0 / 负数 / 天文数字撑爆上下文。"""

    @pytest.mark.parametrize(
        ("given", "expected"),
        [(50, 50), (0, 1), (-5, 1), (10_000, 500), (500, 500), (1, 1)],
    )
    def test_clamps_into_range(self, given: int, expected: int) -> None:
        assert clamp_max_rows(given) == expected

    @pytest.mark.parametrize("given", ["abc", None, [], {}])
    def test_unparseable_falls_back_to_default(self, given: object) -> None:
        assert clamp_max_rows(given) == 120


class TestMacroRoutes:
    """宏观路由表——原服务 ``__macro_router__`` 特例的搬运，须逐条对住。"""

    def test_routes_match_migrated_akshare_functions(self) -> None:
        assert MACRO_ROUTES == {
            "cpi": "macro_china_cpi",
            "ppi": "macro_china_ppi",
            "gdp": "macro_china_gdp",
            "pmi": "macro_china_pmi",
        }

    def test_unknown_indicator_returns_bad_request_without_touching_network(self) -> None:
        """未知指标必须在取数前短路，且错误里列出支持值。"""
        out = json.loads(ChinaMacroTool().execute(indicator="不存在的指标"))

        assert out["ok"] is False
        assert out["error_kind"] == "bad_request"
        assert "cpi" in out["error"]


class TestRequiredParamGuards:
    """必填参数短路——在触网前就返回清晰错误。"""

    @pytest.mark.parametrize(
        "tool",
        [FundNavTool(), FundPositionTool(), ChinaFuturesTool()],
    )
    def test_missing_symbol_is_bad_request(self, tool: object) -> None:
        out = json.loads(tool.execute())

        assert out["ok"] is False
        assert out["error_kind"] == "bad_request"
        assert "symbol" in out["error"]

    def test_unknown_fund_position_view_is_bad_request(self) -> None:
        out = json.loads(FundPositionTool().execute(symbol="000001", view="乱填"))

        assert out["ok"] is False
        assert out["error_kind"] == "bad_request"
        assert "industry" in out["error"]


class TestEnvelopes:
    """信封形状——分身按这个结构消费，形状漂了就是静默故障。"""

    def test_ok_envelope_shape(self) -> None:
        out = json.loads(ok_envelope(market="China macro", data={"rows": []}))

        assert out["ok"] is True
        assert out["market"] == "China macro"
        assert out["source"] == "akshare"
        assert "warnings" not in out  # 无告警时不应出现空字段

    def test_ok_envelope_carries_warnings_when_given(self) -> None:
        out = json.loads(
            ok_envelope(market="China fund", data={}, warnings=["被截断了"])
        )

        assert out["warnings"] == ["被截断了"]

    def test_error_envelope_names_kind(self) -> None:
        out = json.loads(error_envelope(AkshareCallError("超时了", kind="timeout")))

        assert out["ok"] is False
        assert out["error"] == "超时了"
        assert out["error_kind"] == "timeout"


class TestToolDeclarations:
    """工具声明——名字/必填项是分身可见的契约。"""

    def test_tool_names_are_stable(self) -> None:
        assert ChinaMacroTool.name == "get_china_macro"
        assert FundNavTool.name == "get_fund_nav"
        assert FundPositionTool.name == "get_fund_position"
        assert ChinaFuturesTool.name == "get_futures_daily"

    def test_all_four_are_readonly(self) -> None:
        """四者都是只读数据工具，绝不能被标成写类。"""
        for tool in (ChinaMacroTool, FundNavTool, FundPositionTool, ChinaFuturesTool):
            assert tool.is_readonly is True

    def test_fund_position_declares_three_views(self) -> None:
        views = FundPositionTool.parameters["properties"]["view"]["enum"]

        assert set(views) == {"stock", "industry", "asset"}

    def test_macro_enum_matches_route_table(self) -> None:
        """声明的 enum 必须与路由表一致，否则分身会调到不存在的指标。"""
        declared = set(ChinaMacroTool.parameters["properties"]["indicator"]["enum"])

        assert declared == set(MACRO_ROUTES)


class TestRegistryDiscovery:
    """四个工具必须能被自动发现注册——漏注册 = 分身根本看不见。"""

    def test_all_four_registered_without_shell_tools(self) -> None:
        from src.tools import build_registry

        names = build_registry(include_shell_tools=False).tool_names

        for name in (
            "get_china_macro",
            "get_fund_nav",
            "get_fund_position",
            "get_futures_daily",
        ):
            assert name in names
