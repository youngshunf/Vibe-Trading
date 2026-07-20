"""金融回测延迟基准的稳定统计契约测试。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "benchmark_backtest_latency.py"
SPEC = importlib.util.spec_from_file_location("benchmark_backtest_latency", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_nearest_rank_has_explicit_p95_and_p99_semantics() -> None:
    samples = list(range(1, 101))

    assert benchmark.nearest_rank(samples, 50) == 50
    assert benchmark.nearest_rank(samples, 95) == 95
    assert benchmark.nearest_rank(samples, 99) == 99


def test_summary_requires_enough_real_samples_and_never_exposes_run_path() -> None:
    report = {
        "schema_version": "finance_backtest_report_v1",
        "engine_version": "0.1.11",
        "data_source": "tencent",
        "benchmark": {"symbol": "UNIVERSE_EW"},
        "strategy_code_sha256": "a" * 64,
    }
    config = {
        "codes": ["600519.SH"],
        "start_date": "2025-01-02",
        "end_date": "2025-06-30",
        "source": "auto",
        "interval": "1D",
        "engine": "daily",
    }

    with pytest.raises(ValueError, match="至少需要 20"):
        benchmark.summarize_measurements(
            [1.0] * 19,
            report,
            config,
            started_at="2026-07-19T00:00:00+00:00",
            git_revision="abc",
        )

    summary = benchmark.summarize_measurements(
        [float(value) for value in range(1, 21)],
        report,
        config,
        started_at="2026-07-19T00:00:00+00:00",
        git_revision="abc",
    )
    assert summary["latency_seconds"]["p95"] == 19
    assert summary["latency_seconds"]["p99"] == 20
    assert "run_dir" not in str(summary)
