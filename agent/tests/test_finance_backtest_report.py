"""金融回测稳定产物契约测试。"""

from __future__ import annotations

import hashlib
import json

import pytest

from backtest.engines.china_a import ChinaAEngine
from backtest.finance_report import write_finance_backtest_report


def _write_artifacts(run_dir) -> None:
    artifacts = run_dir / "artifacts"
    artifacts.mkdir(parents=True)
    (artifacts / "equity.csv").write_text(
        "timestamp,ret,equity,drawdown,benchmark_equity,active_ret\n"
        "2025-01-02,0,1000000,0,1000000,0\n"
        "2025-01-03,0.01,1010000,0,1005000,0.005\n",
        encoding="utf-8",
    )
    (artifacts / "trades.csv").write_text(
        "timestamp,code,side,price,qty,reason,pnl,holding_days,return_pct\n"
        "2025-01-02,600519.SH,buy,1500,100,signal,0,0,0\n"
        "2025-01-03,600519.SH,sell,1515,100,signal,1400,1,0.9333\n",
        encoding="utf-8",
    )


def _config() -> dict:
    return {
        "title": "贵州茅台双均线回测",
        "codes": ["600519.SH"],
        "start_date": "2025-01-02",
        "end_date": "2025-01-03",
        "initial_cash": 1_000_000,
        "engine": "daily",
        "source": "tushare",
    }


def _metrics() -> dict:
    return {
        "annual_return": 0.18,
        "sharpe": 1.23,
        "max_drawdown": -0.08,
        "win_rate": 0.55,
        "trade_count": 1,
        "benchmark_return": 0.12,
        "sortino": 1.5,
        "by_symbol": {"600519.SH": {"count": 1}},
    }


def test_finance_report_records_actual_a_share_assumptions(tmp_path) -> None:
    run_dir = tmp_path / "run-1"
    _write_artifacts(run_dir)
    strategy = run_dir / "code" / "signal_engine.py"
    strategy.parent.mkdir()
    strategy.write_text("class SignalEngine:\n    pass\n", encoding="utf-8")

    engine = ChinaAEngine(_config())
    report = write_finance_backtest_report(
        run_dir,
        _config(),
        _metrics(),
        data_sources=["tencent"],
        cost_model=engine.finance_cost_model(),
        benchmark_symbol="000300.SH",
        engine_version="0.1.11",
    )

    assert report["schema_version"] == "finance_backtest_report_v1"
    assert report["period"] == {"start": "2025-01-02", "end": "2025-01-03"}
    assert report["universe"] == ["600519.SH"]
    assert report["initial_capital"] == 1_000_000
    assert report["benchmark"] == {"symbol": "000300.SH", "return": 0.12}
    assert report["engine_version"] == "0.1.11"
    assert report["data_source"] == "tencent"
    assert report["cost_model"] == {
        "market_engine": "ChinaAEngine",
        "parameters": {
            "commission_min": 5.0,
            "commission_rate": 0.00025,
            "default_leverage": 1.0,
            "slippage_rate": 0.001,
            "stamp_tax": 0.0005,
            "transfer_fee": 0.00001,
        },
    }
    assert report["strategy_code_sha256"] == hashlib.sha256(strategy.read_bytes()).hexdigest()
    assert report["metrics"]["sortino"] == 1.5
    assert report["metrics"]["by_symbol"]["600519.SH"]["count"] == 1
    assert report["equity_curve"][1]["equity"] == 1_010_000
    assert report["trades"][1]["pnl"] == 1_400
    assert str(tmp_path) not in json.dumps(report, ensure_ascii=False)
    assert json.loads((run_dir / "finance_backtest_report.json").read_text(encoding="utf-8")) == report


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("start_date", "", "样本开始日期"),
        ("end_date", "", "样本结束日期"),
        ("codes", [], "回测标的"),
    ],
)
def test_finance_report_rejects_missing_honesty_fields(tmp_path, field: str, value, message: str) -> None:
    run_dir = tmp_path / "run-2"
    _write_artifacts(run_dir)
    strategy = run_dir / "code" / "signal_engine.py"
    strategy.parent.mkdir()
    strategy.write_text("class SignalEngine:\n    pass\n", encoding="utf-8")
    config = _config()
    config[field] = value

    with pytest.raises(ValueError, match=message):
        write_finance_backtest_report(
            run_dir,
            config,
            _metrics(),
            data_sources=["tencent"],
            cost_model=ChinaAEngine(_config()).finance_cost_model(),
            benchmark_symbol="000300.SH",
            engine_version="0.1.11",
        )


@pytest.mark.parametrize(
    ("data_sources", "benchmark_symbol", "engine_version", "message"),
    [
        ([], "000300.SH", "0.1.11", "实际数据源"),
        (["tencent"], "", "0.1.11", "实际基准"),
        (["tencent"], "000300.SH", "", "引擎版本"),
    ],
)
def test_finance_report_rejects_missing_execution_evidence(
    tmp_path,
    data_sources: list[str],
    benchmark_symbol: str,
    engine_version: str,
    message: str,
) -> None:
    run_dir = tmp_path / "run-3"
    _write_artifacts(run_dir)
    strategy = run_dir / "code" / "signal_engine.py"
    strategy.parent.mkdir()
    strategy.write_text("class SignalEngine:\n    pass\n", encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        write_finance_backtest_report(
            run_dir,
            _config(),
            _metrics(),
            data_sources=data_sources,
            cost_model=ChinaAEngine(_config()).finance_cost_model(),
            benchmark_symbol=benchmark_symbol,
            engine_version=engine_version,
        )
