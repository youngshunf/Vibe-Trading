"""生成可由唤星稳定收割的金融回测业务产物。"""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "finance_backtest_report_v1"
REPORT_FILE_NAME = "finance_backtest_report.json"
_PERFORMANCE_FIELDS = (
    "annual_return",
    "sharpe",
    "max_drawdown",
    "win_rate",
    "trade_count",
)


def write_finance_backtest_report(
    run_dir: Path,
    config: Mapping[str, Any],
    metrics: Mapping[str, Any],
    *,
    data_sources: Sequence[str],
    cost_model: Mapping[str, Any],
    benchmark_symbol: str,
    engine_version: str,
) -> dict[str, Any]:
    """从本次真实执行配置和产物生成稳定回测报告。"""
    run_dir = Path(run_dir)
    period_start = _required_date(config.get("start_date"), "样本开始日期")
    period_end = _required_date(config.get("end_date"), "样本结束日期")
    if period_start > period_end:
        raise ValueError("样本开始日期不得晚于结束日期")

    universe = _required_strings(config.get("codes"), "回测标的")
    initial_capital = _required_positive_number(config.get("initial_cash"), "初始资金")
    source = _single_data_source(data_sources)
    benchmark = _required_text(benchmark_symbol, "实际基准", max_length=16)
    version = _required_text(engine_version, "引擎版本", max_length=32)
    normalized_cost_model = _normalize_cost_model(cost_model)
    normalized_metrics = _json_safe(dict(metrics))
    performance = {field: _required_metric(normalized_metrics, field) for field in _PERFORMANCE_FIELDS}
    benchmark_return = _required_metric(normalized_metrics, "benchmark_return")

    strategy_path = run_dir / "code" / "signal_engine.py"
    if not strategy_path.is_file():
        raise ValueError("缺少策略源码 code/signal_engine.py")
    strategy_hash = _sha256(strategy_path)

    title = str(config.get("title") or f"{'、'.join(universe)} 回测报告").strip()
    if not title or len(title) > 256:
        raise ValueError("回测报告标题为空或超过 256 个字符")

    report = {
        "schema_version": SCHEMA_VERSION,
        "title": title,
        "period": {
            "start": period_start.isoformat(),
            "end": period_end.isoformat(),
        },
        "universe": universe,
        "initial_capital": initial_capital,
        "cost_model": normalized_cost_model,
        "benchmark": {
            "symbol": benchmark,
            "return": benchmark_return,
        },
        "performance": performance,
        "metrics": normalized_metrics,
        "equity_curve": _read_csv_rows(run_dir / "artifacts" / "equity.csv", "artifacts/equity.csv"),
        "trades": _read_csv_rows(run_dir / "artifacts" / "trades.csv", "artifacts/trades.csv"),
        "engine_version": version,
        "data_source": source,
        "strategy_code_sha256": strategy_hash,
    }
    encoded = json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        allow_nan=False,
    )
    target = run_dir / REPORT_FILE_NAME
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(encoded + "\n", encoding="utf-8")
    os.replace(temporary, target)
    return report


def _required_date(value: Any, label: str) -> date:
    text = _required_text(value, label)
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{label}必须是 YYYY-MM-DD") from error


def _required_strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{label}必须是非空字符串数组")
    values = [str(item).strip() for item in value]
    if not values or any(not item for item in values):
        raise ValueError(f"{label}必须是非空字符串数组")
    return values


def _required_text(value: Any, label: str, *, max_length: int | None = None) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"缺少{label}")
    if max_length is not None and len(text) > max_length:
        raise ValueError(f"{label}超过 {max_length} 个字符")
    return text


def _required_positive_number(value: Any, label: str) -> int | float:
    normalized = _finite_number(value, label)
    if normalized <= 0:
        raise ValueError(f"{label}必须大于 0")
    return normalized


def _required_metric(metrics: Mapping[str, Any], field: str) -> int | float:
    if field not in metrics:
        raise ValueError(f"回测指标缺少 {field}")
    return _finite_number(metrics[field], f"回测指标 {field}")


def _finite_number(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label}必须是有限数值")
    if not math.isfinite(float(value)):
        raise ValueError(f"{label}必须是有限数值")
    return value


def _single_data_source(data_sources: Sequence[str]) -> str:
    sources = [str(source).strip() for source in data_sources if str(source).strip()]
    if not sources:
        raise ValueError("缺少本次实际数据源")
    unique_sources = list(dict.fromkeys(sources))
    if len(unique_sources) != 1:
        raise ValueError("一次金融回测必须能归因到唯一实际数据源")
    return _required_text(unique_sources[0], "实际数据源", max_length=32)


def _normalize_cost_model(cost_model: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(cost_model, Mapping):
        raise ValueError("实际成本模型必须是对象")
    normalized = _json_safe(dict(cost_model))
    parameters = normalized.get("parameters")
    if (
        not isinstance(normalized.get("market_engine"), str)
        or not normalized["market_engine"].strip()
        or not isinstance(parameters, dict)
        or not parameters
    ):
        raise ValueError("实际成本模型缺少引擎或运行参数")
    return normalized


def _read_csv_rows(path: Path, logical_name: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"缺少回测产物 {logical_name}")
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"回测产物 {logical_name} 缺少表头")
        return [{str(key): _csv_value(value) for key, value in row.items()} for row in reader]


def _csv_value(value: str | None) -> Any:
    if value is None or value == "":
        return None
    try:
        integer = int(value)
        if str(integer) == value or (value.startswith("+") and str(integer) == value[1:]):
            return integer
    except ValueError:
        pass
    try:
        number = float(value)
    except ValueError:
        return value
    return number if math.isfinite(number) else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        return _json_safe(value.item())
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise ValueError(f"回测产物含不可序列化类型：{type(value).__name__}")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
