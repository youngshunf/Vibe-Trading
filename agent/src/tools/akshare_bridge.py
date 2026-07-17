"""中国市场 akshare 取数桥 —— 唤星 fork 新增（非上游代码）。

背景：上游引擎的 loader 层（``backtest/loaders/``）是 OHLCV 导向的，
``FALLBACK_CHAINS`` 虽已声明 ``macro`` / ``fund`` / ``futures`` 三条链，但
akshare loader 的 ``_fetch_one`` 只按标的形态分派 A股/美股/港股/ETF/外汇，
**没有**宏观指标、基金净值、基金持仓这三类非 OHLCV 语义的分支。

因此这四类中国市场数据（中国宏观 / 基金净值 / 基金持仓 / 国内期货）从
``finance-data-service`` 原样搬运其**已验证的 akshare 调用**（同函数、同 kwargs、
同 akshare 1.18.64），落在本模块统一收口：唯一 import akshare 的地方。

横切关注点忠实对齐原服务 ``app/akshare_source.py``：
① 懒 import（避免启动期初始化 JS 引擎拖慢 worker）；
② 超时包同步调用；
③ 并发上限 + 归一异常（绝不透 traceback 给分身）。

本模块不含 ``BaseTool`` 子类，故不会被 ``_discover_subclasses`` 注册成工具
（与同目录的 ``tushare_fallbacks`` / ``redaction`` / ``path_utils`` 同类）。
"""

from __future__ import annotations

import json
import logging
import math
import threading
from typing import Any

logger = logging.getLogger(__name__)

# akshare 同步调用的超时上限（秒）。与原 finance-data-service 默认值一致。
_TIMEOUT_SECONDS = 25

# 并发上限：akshare 多为无鉴权公开源，突发并发会被上游封 IP。
# 原服务用 asyncio.Semaphore(4)；此处工具面是同步的，改用等价的线程信号量。
_CONCURRENCY = 4
_semaphore = threading.BoundedSemaphore(_CONCURRENCY)

# 单次返回的最大行数。宏观序列（如 CPI 月度自 2008 年起）动辄数百行，
# 全量回灌会挤爆分身上下文，故按「最近 N 行」截断并在信封里明示。
_DEFAULT_MAX_ROWS = 120
_MAX_ROWS_CEILING = 500

# ``get_china_macro`` 的 indicator → akshare 函数名路由表。
# 这是 finance-data-service ``interfaces.py::MACRO_ROUTES`` 的原样搬运——
# 它在原服务里是 ``ak_func="__macro_router__"`` 特例，路由逻辑在服务层按
# indicator 分发，不是单一 akshare 函数，故搬运时连路由表一起搬。
MACRO_ROUTES: dict[str, str] = {
    "cpi": "macro_china_cpi",
    "ppi": "macro_china_ppi",
    "gdp": "macro_china_gdp",
    "pmi": "macro_china_pmi",
}


class AkshareCallError(Exception):
    """akshare 调用失败（限流 / 接口变更 / 网络 / 超时）。

    Attributes:
        kind: 失败类别，用于信封里区分 ``bad_request`` / ``timeout`` /
            ``missing_function`` / 上游异常类型名。
    """

    def __init__(self, message: str, *, kind: str = "upstream_error") -> None:
        super().__init__(message)
        self.kind = kind


def _load_akshare() -> Any:
    """懒加载 akshare 模块。

    Returns:
        akshare 模块对象。

    Raises:
        AkshareCallError: akshare 未安装时（依赖缺失须显式报错，
            不做静默降级——静默降级正是 R9 记档的系统性幻觉根因）。
    """
    try:
        import akshare as ak  # noqa: PLC0415 — 懒 import：首次调用才初始化 JS 引擎
    except ImportError as exc:  # pragma: no cover - 依赖缺失是部署问题
        raise AkshareCallError(
            f"akshare 未安装，无法取中国市场数据：{exc}", kind="missing_dependency"
        ) from exc
    return ak


def resolve_akshare_callable(func_name: str) -> Any:
    """按函数名解析 akshare 可调用对象。

    Args:
        func_name: akshare 函数名，如 ``macro_china_cpi``。

    Returns:
        对应的可调用对象。

    Raises:
        AkshareCallError: akshare 缺该函数（上游接口变更）。
    """
    ak = _load_akshare()
    fn = getattr(ak, func_name, None)
    if fn is None:
        raise AkshareCallError(
            f"akshare 缺函数 {func_name}（接口可能已变更）", kind="missing_function"
        )
    return fn


def call_akshare(func_name: str, kwargs: dict[str, Any]) -> Any:
    """执行一次带并发上限与超时的 akshare 调用。

    Args:
        func_name: akshare 函数名。
        kwargs: 传给该函数的关键字参数（调用方须已合并默认值）。

    Returns:
        akshare 返回的 DataFrame。

    Raises:
        AkshareCallError: 超时或上游异常（已归一，不含 traceback）。
    """
    fn = resolve_akshare_callable(func_name)

    result: dict[str, Any] = {}

    def _call() -> None:
        try:
            result["value"] = fn(**kwargs)
        except BaseException as exc:  # noqa: BLE001 — 归一所有上游异常
            result["error"] = exc

    with _semaphore:  # 并发上限（防上游封 IP）
        worker = threading.Thread(target=_call, daemon=True, name=f"akshare-{func_name}")
        worker.start()
        worker.join(timeout=_TIMEOUT_SECONDS)

    if worker.is_alive():
        # 线程无法强杀；标 daemon 后随进程退出，此处只对调用方报超时。
        raise AkshareCallError(
            f"akshare 调用超时（>{_TIMEOUT_SECONDS}s）：{func_name}", kind="timeout"
        )
    if "error" in result:
        exc = result["error"]
        raise AkshareCallError(str(exc)[:200], kind=type(exc).__name__)
    return result.get("value")


def clamp_max_rows(value: Any) -> int:
    """把调用方给的 ``max_rows`` 夹到 ``[1, _MAX_ROWS_CEILING]``。

    Args:
        value: 原始入参（任意类型）。

    Returns:
        合法的行数上限；不可解析时回落默认值。
    """
    try:
        rows = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_ROWS
    if rows < 1:
        return 1
    if rows > _MAX_ROWS_CEILING:
        return _MAX_ROWS_CEILING
    return rows


def _jsonable(value: Any) -> Any:
    """把单个 DataFrame 单元格转成可 JSON 序列化的值。

    Args:
        value: 原始单元格（可能是 numpy 标量 / Timestamp / NaN）。

    Returns:
        原生 Python 值；NaN / NaT 归一为 ``None``。
    """
    if value is None:
        return None
    # pandas 的缺失值（NaN/NaT）不能直接进 JSON，且 float('nan') 非法。
    if isinstance(value, float) and math.isnan(value):
        return None
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return _jsonable(item())
        except (ValueError, TypeError):
            pass
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if math.isnan(value) else value
    return str(value)


def frame_to_rows(
    frame: Any, max_rows: int, *, newest_first: bool
) -> tuple[list[dict[str, Any]], int]:
    """截取 DataFrame 中**最近的** ``max_rows`` 行，转成 JSON 友好的行列表。

    ⚠️ 各上游的行序并不统一，必须逐源实测后声明，不能一律 ``tail()``：
    ``macro_china_*`` 与 ``fund_portfolio_industry_allocation_em`` 是**新→旧**
    （取 ``head``），``fund_open_fund_info_em`` 与 ``futures_zh_daily_sina`` 是
    **旧→新**（取 ``tail``）。搞反了会静默返回最古老的数据——2008 年的 CPI 当
    成最新月份，比报错更难查。

    不重排行序：保持上游原序返回，由调用方在信封里声明 ``order``，避免把
    行业配置那种「按报告期分组 + 组内序号」的表反转成乱序。

    Args:
        frame: akshare 返回的 DataFrame（也容忍 None / 空）。
        max_rows: 保留的最大行数。
        newest_first: 上游行序是否为新→旧。``True`` 取 ``head``，``False`` 取 ``tail``。

    Returns:
        ``(rows, total_rows)``——截断后的行列表，以及截断前的总行数。
    """
    if frame is None:
        return [], 0
    try:
        total = int(len(frame))
    except TypeError:
        return [], 0
    if total == 0:
        return [], 0

    recent = frame.head(max_rows) if newest_first else frame.tail(max_rows)
    rows: list[dict[str, Any]] = []
    for record in recent.to_dict(orient="records"):
        rows.append({str(key): _jsonable(val) for key, val in record.items()})
    return rows, total


def ok_envelope(
    *,
    market: str,
    data: dict[str, Any],
    warnings: list[str] | None = None,
) -> str:
    """构造成功信封，形状对齐引擎内既有只读数据工具。

    Args:
        market: 市场标识，如 ``China A`` / ``China macro``。
        data: 业务载荷。
        warnings: 可选告警（如结果被截断）。

    Returns:
        JSON 字符串信封。
    """
    envelope: dict[str, Any] = {
        "ok": True,
        "market": market,
        "source": "akshare",
        "data": data,
    }
    if warnings:
        envelope["warnings"] = warnings
    return json.dumps(envelope, ensure_ascii=False)


def error_envelope(exc: Exception) -> str:
    """构造失败信封。

    Args:
        exc: 触发失败的异常。

    Returns:
        JSON 字符串信封 ``{"ok": false, "error": ..., "error_kind": ...}``。
    """
    kind = exc.kind if isinstance(exc, AkshareCallError) else type(exc).__name__
    return json.dumps(
        {"ok": False, "error": str(exc), "error_kind": kind},
        ensure_ascii=False,
    )
