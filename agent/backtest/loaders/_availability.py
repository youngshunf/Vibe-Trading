"""数据源链路可用性探针 —— 唤星 fork 新增（非上游代码）。

**这个模块存在的唯一理由：消灭「以为有 N 源兜底、实际只有 M 源」的系统性幻觉。**

根因（R9）：所有 loader 的 ``is_available()`` 都是 ``bool`` 语义，且多数实现是
``try: import xxx / except ImportError: return False``——**依赖缺失不报错、链默默变短**。
``resolve_loader`` walk 链时，构造失败只记 ``logger.debug``，``is_available()`` 返
``False`` 更是**连日志都没有**，直接 ``continue``。于是：

- ``FALLBACK_CHAINS['a_share']`` 声明 7 源，实测**只有 3 源**真能用；
- ``macro`` / ``fund`` / ``futures`` 三条链声明 3 源，实测**只有 akshare 一根独苗**
  （tushare 需 token、local 需 Data Bridge 配置）——这三条链正是唤星
  ``get_china_macro`` / ``get_fund_nav`` / ``get_fund_position`` / ``get_futures_daily``
  的取数域，**akshare 挂了就是全挂，没有任何兜底**。

链短本身不是 bug（免费源本就按需装），**看不见才是 bug**。故本模块提供
``probe_chain()``：把每一源「能不能用、为什么不能用」显式列出来，让「实际几源」
可核实、而不是靠读 ``FALLBACK_CHAINS`` 猜。

设计取舍：
- **不改上游各 loader**：``is_available()`` 只回 ``bool``、拿不到原因。若给每个
  loader 加 ``unavailable_reason()``，要动十几个上游文件、每次 sync 都冲突。
  改为在本模块集中维护「装法提示表」，上游文件零改动。
- **不提级日志**：跳过一个不可用源是**预期且可恢复**的（这正是兜底链的语义），
  按日志分级铁律记 ``debug``；只有整条链全灭（``NoAvailableSourceError``）才是终局。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 各源不可用时的「怎么才能用」提示。只用于诊断输出，不参与任何取数决策。
#
# 键 = loader 注册名；值 = 人类可读的启用方式。缺键的源回落通用文案，
# 不影响正确性——本表是「说得更清楚」，不是「判断依据」。
_ENABLEMENT_HINTS: dict[str, str] = {
    "mootdx": (
        "需手动 pip install mootdx；**刻意未进引擎依赖**——最新版 mootdx 锁 "
        "httpx<0.28 / tenacity<9，与引擎主依赖声明的 httpx>=0.28.0 冲突，"
        "装它会把全局 httpx 降到 0.25.2（langchain-openai / fastapi / 引擎 _http "
        "共用这个底座）。详见验收 A3 决策记录"
    ),
    "baostock": "需装引擎 optional extra：uv pip install -e '.[ashare]'（TCP 协议，绕 HTTP CDN 封锁）",
    "tushare": "需配置 TUSHARE_TOKEN（未配置时 SDK 会在 __init__ 直接抛错）",
    "futu": "需装 futu SDK 且 FutuOpenD 网关可达（配 futu_host / futu_port）",
    "longbridge": "需装 optional extra longbridge + 配 app_key / app_secret / access_token",
    "india_broker": "需装并配置某个印度券商 SDK",
    "local": "需配置 Data Bridge：~/.vibe-trading/data-bridge/config.yaml 且 sources 非空",
    "finnhub": "需配置 FINNHUB_API_KEY",
    "alphavantage": "需配置 ALPHAVANTAGE_API_KEY",
    "tiingo": "需配置 TIINGO_API_KEY",
    "fmp": "需配置 FMP_API_KEY",
    "qveris": "需开启付费 QVeris 路由并配 api_key（mode=paid）",
}


@dataclass(frozen=True)
class SourceProbe:
    """单个数据源的可用性实况。

    Attributes:
        name: loader 注册名。
        available: 是否真的可用（已实际调用过 ``is_available()``）。
        reason: 不可用的原因；可用时为 ``None``。
    """

    name: str
    available: bool
    reason: str | None = None


@dataclass(frozen=True)
class ChainProbe:
    """一条兜底链的实况：声明了几源、实际活几源。

    Attributes:
        market: 市场键，如 ``a_share``。
        sources: 逐源探测结果，顺序与 ``FALLBACK_CHAINS`` 一致。
    """

    market: str
    sources: tuple[SourceProbe, ...]

    @property
    def declared_count(self) -> int:
        """链里声明了多少源。"""
        return len(self.sources)

    @property
    def live_count(self) -> int:
        """实际有多少源可用——这才是真正的兜底深度。"""
        return sum(1 for s in self.sources if s.available)

    @property
    def is_degraded(self) -> bool:
        """是否「声明的比实际多」——即存在静默降级。"""
        return self.live_count < self.declared_count

    @property
    def has_no_fallback(self) -> bool:
        """是否已无兜底可言（0 或 1 源）——独苗挂了就是全挂。"""
        return self.live_count <= 1

    def describe(self) -> str:
        """渲染成人类可读的一行摘要 + 逐源明细。"""
        head = f"{self.market}: 声明 {self.declared_count} 源，实际可用 {self.live_count} 源"
        if self.has_no_fallback:
            head += "  ⚠️ 无兜底（独苗或全灭）"
        elif self.is_degraded:
            head += "  ⚠️ 存在静默降级"
        lines = [head]
        for probe in self.sources:
            if probe.available:
                lines.append(f"  ✓ {probe.name}")
            else:
                lines.append(f"  ✗ {probe.name} —— {probe.reason}")
        return "\n".join(lines)


def _hint_for(name: str) -> str:
    """取某源的启用提示，缺失时回落通用文案。"""
    return _ENABLEMENT_HINTS.get(name, "不可用（依赖未安装 / 凭据未配置 / 配置缺失）")


def probe_source(name: str, registry: dict[str, Any]) -> SourceProbe:
    """探测单个源的真实可用性。

    ⚠️ 会真的实例化 loader 并调用其 ``is_available()``——这正是要点：
    只有真调用才知道真答案，读 ``FALLBACK_CHAINS`` 只能读到「声明」。
    各 loader 的 ``is_available()`` 契约上是无副作用的轻量检查
    （见 ``base.py``；longbridge 的实现注释明确承诺不消耗行情配额）。

    Args:
        name: loader 注册名。
        registry: loader 注册表（``LOADER_REGISTRY``）。

    Returns:
        该源的探测结果。
    """
    cls = registry.get(name)
    if cls is None:
        return SourceProbe(name, False, "未注册（loader 模块未导入或不存在）")

    # 部分 loader（如 tushare）在 __init__ 里就调 SDK、缺凭据直接抛——
    # 与 is_available()=False 同等对待，但要把异常信息带出来，
    # 否则「为什么这源是黑的」永远查不到。
    try:
        loader = cls()
    except Exception as exc:  # noqa: BLE001 — 构造失败的原因必须原样带出
        detail = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return SourceProbe(name, False, f"构造失败（{detail[:120]}）；{_hint_for(name)}")

    try:
        ok = bool(loader.is_available())
    except Exception as exc:  # noqa: BLE001 — 探测本身不该炸；炸了也要诚实报告
        return SourceProbe(name, False, f"可用性探测抛错（{type(exc).__name__}）；{_hint_for(name)}")

    return SourceProbe(name, True) if ok else SourceProbe(name, False, _hint_for(name))


def probe_chain(market: str, chains: dict[str, list[str]], registry: dict[str, Any]) -> ChainProbe:
    """探测某市场兜底链的真实深度。

    Args:
        market: 市场键，如 ``a_share`` / ``macro``。
        chains: 兜底链表（``FALLBACK_CHAINS``）。
        registry: loader 注册表（``LOADER_REGISTRY``）。

    Returns:
        该链的实况。未知市场返回空链（``declared_count == 0``）。
    """
    return ChainProbe(
        market=market,
        sources=tuple(probe_source(name, registry) for name in chains.get(market, [])),
    )


def format_unavailable_reasons(probe: ChainProbe) -> str:
    """把「整条链为何全灭」渲染成可直接进异常消息的文本。

    ``NoAvailableSourceError`` 原本只说 "Tried: [...]. Check network and API token
    config."——列了名字却不说**每个为什么不行**，等于让人重新排查一遍。

    Args:
        probe: 链探测结果。

    Returns:
        逐源原因的多行文本。
    """
    return "\n".join(f"  - {s.name}: {s.reason}" for s in probe.sources if not s.available)
