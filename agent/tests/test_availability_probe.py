"""链路可用性探针的单元测试 —— 唤星 fork 新增（非上游代码）。

对应验收 R9（静默降级排查）与 A3（mootdx 取舍）。全部不触网：用真实的
``probe_*`` 代码路径 + 就地定义的假 loader 类（**不是 mock**——它们是真类、
真被实例化、真调 ``is_available()``，只是不联网），以及真实的 registry 常量。
"""

from __future__ import annotations

import pytest

from backtest.loaders._availability import (
    SourceProbe,
    format_unavailable_reasons,
    probe_chain,
    probe_source,
)

pytestmark = pytest.mark.unit


class _LiveLoader:
    """可用的源。"""

    def is_available(self) -> bool:
        return True


class _DarkLoader:
    """依赖/凭据缺失的源——上游最常见的形态：吞掉原因只回 False。"""

    def is_available(self) -> bool:
        return False


class _ExplodingInitLoader:
    """构造即炸的源（tushare 缺 token 的真实形态）。"""

    def __init__(self) -> None:
        raise RuntimeError("请设置tushare pro的token凭证码")

    def is_available(self) -> bool:  # pragma: no cover — 到不了
        return False


class _ExplodingProbeLoader:
    """探测本身抛错的源。"""

    def is_available(self) -> bool:
        raise ConnectionError("网络不可达")


class TestProbeSource:
    """单源探测——每种「黑掉」的方式都必须给出可读原因，不能只回 False。"""

    def test_live_source_has_no_reason(self) -> None:
        probe = probe_source("live", {"live": _LiveLoader})

        assert probe == SourceProbe("live", True, None)

    def test_unregistered_source_says_so(self) -> None:
        probe = probe_source("nope", {})

        assert probe.available is False
        assert "未注册" in probe.reason

    def test_dark_source_carries_enablement_hint(self) -> None:
        """不可用时必须说「怎么才能用」，而不是干巴巴一个 False。"""
        probe = probe_source("baostock", {"baostock": _DarkLoader})

        assert probe.available is False
        assert "ashare" in probe.reason  # 指出该装哪个 extra

    def test_unknown_dark_source_falls_back_to_generic_reason(self) -> None:
        """提示表没收录的源也要有话说，不能返回 None。"""
        probe = probe_source("some_new_loader", {"some_new_loader": _DarkLoader})

        assert probe.available is False
        assert probe.reason

    def test_construct_failure_surfaces_the_exception_text(self) -> None:
        """构造失败的原因必须原样带出——否则「为什么这源是黑的」永远查不到。"""
        probe = probe_source("tushare", {"tushare": _ExplodingInitLoader})

        assert probe.available is False
        assert "构造失败" in probe.reason
        assert "token" in probe.reason
        assert "TUSHARE_TOKEN" in probe.reason  # 同时给出启用方式

    def test_probe_failure_is_reported_not_raised(self) -> None:
        """探测抛错不能把整个诊断带崩——诊断的职责是报告，不是传播。"""
        probe = probe_source("flaky", {"flaky": _ExplodingProbeLoader})

        assert probe.available is False
        assert "ConnectionError" in probe.reason


class TestProbeChain:
    """整链探测——「声明 N 源、实际 M 源」必须可核实。"""

    def test_counts_declared_vs_live(self) -> None:
        chains = {"a_share": ["live", "dark", "live2"]}
        registry = {"live": _LiveLoader, "dark": _DarkLoader, "live2": _LiveLoader}

        probe = probe_chain("a_share", chains, registry)

        assert probe.declared_count == 3
        assert probe.live_count == 2
        assert probe.is_degraded is True

    def test_full_chain_is_not_degraded(self) -> None:
        chains = {"crypto": ["live", "live2"]}
        registry = {"live": _LiveLoader, "live2": _LiveLoader}

        probe = probe_chain("crypto", chains, registry)

        assert probe.is_degraded is False
        assert probe.has_no_fallback is False

    def test_single_live_source_means_no_fallback(self) -> None:
        """独苗 = 没有兜底。macro/fund/futures 三条链的真实处境。"""
        chains = {"macro": ["live", "dark", "dark2"]}
        registry = {"live": _LiveLoader, "dark": _DarkLoader, "dark2": _DarkLoader}

        probe = probe_chain("macro", chains, registry)

        assert probe.live_count == 1
        assert probe.has_no_fallback is True

    def test_all_dark_chain_is_no_fallback(self) -> None:
        chains = {"x": ["dark"]}
        probe = probe_chain("x", chains, {"dark": _DarkLoader})

        assert probe.live_count == 0
        assert probe.has_no_fallback is True

    def test_unknown_market_yields_empty_chain(self) -> None:
        probe = probe_chain("不存在的市场", {}, {})

        assert probe.declared_count == 0
        assert probe.sources == ()

    def test_probe_order_follows_chain_order(self) -> None:
        """顺序必须与兜底链一致——否则读者会以为优先级是别的。"""
        chains = {"m": ["a", "b", "c"]}
        registry = {"a": _DarkLoader, "b": _LiveLoader, "c": _LiveLoader}

        probe = probe_chain("m", chains, registry)

        assert [s.name for s in probe.sources] == ["a", "b", "c"]


class TestDescribe:
    """诊断渲染——人得看得懂。"""

    def test_describe_flags_degraded_chain(self) -> None:
        chains = {"a_share": ["live", "dark"]}
        text = probe_chain("a_share", chains, {"live": _LiveLoader, "dark": _DarkLoader}).describe()

        assert "声明 2 源，实际可用 1 源" in text
        assert "✓ live" in text
        assert "✗ dark" in text

    def test_describe_marks_no_fallback(self) -> None:
        chains = {"macro": ["live", "dark"]}
        text = probe_chain("macro", chains, {"live": _LiveLoader, "dark": _DarkLoader}).describe()

        assert "无兜底" in text

    def test_format_unavailable_reasons_lists_only_dark_sources(self) -> None:
        chains = {"m": ["live", "dark"]}
        probe = probe_chain("m", chains, {"live": _LiveLoader, "dark": _DarkLoader})

        text = format_unavailable_reasons(probe)

        assert "live" not in text
        assert "dark" in text


class TestRealChainsAreProbeable:
    """对真实 registry 常量的守卫——探针必须能覆盖每条真链，不能漏市场。"""

    def test_every_declared_market_is_probeable(self) -> None:
        from backtest.loaders.registry import FALLBACK_CHAINS, LOADER_REGISTRY, _ensure_registered

        _ensure_registered()

        for market in FALLBACK_CHAINS:
            probe = probe_chain(market, FALLBACK_CHAINS, LOADER_REGISTRY)

            assert probe.declared_count > 0, f"{market} 链为空"
            # 每个不可用的源都必须有原因，不许出现「黑了但不说为什么」
            for source in probe.sources:
                if not source.available:
                    assert source.reason, f"{market}/{source.name} 不可用却没给原因"

    def test_mootdx_is_declared_but_not_installable_by_default(self) -> None:
        """A3 决策钉子：mootdx 在链里声明着，但**刻意不进引擎依赖**。

        它锁 httpx<0.28 / tenacity<9，与引擎主依赖声明的 httpx>=0.28.0 冲突——
        装它会把全局 httpx 降到 0.25.2（langchain-openai / fastapi / 引擎 _http
        共用）。故保留 loader 与链条目（用户可自行 pip install 启用），但默认不可用。

        本测试同时是**反向守卫**：哪天有人把 mootdx 加进主依赖，这里会红，
        提醒他先复核 httpx 冲突是否已被上游解决。
        """
        from backtest.loaders.registry import FALLBACK_CHAINS

        assert "mootdx" in FALLBACK_CHAINS["a_share"], "mootdx 仍应留在链里（用户可自行安装启用）"

        try:
            import mootdx  # noqa: F401
        except ImportError:
            return  # 预期路径：未安装 → 链自然短一格，且 probe 会说明原因

        pytest.fail(
            "mootdx 已安装——若是有意加进引擎依赖，请先复核它与 httpx>=0.28.0 的冲突"
            "（见 A3 决策记录），并更新本测试"
        )
