"""对真实金融回测链路采样并输出可审计的 P95/P99 延迟证据。"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import platform
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence


MIN_SAMPLE_COUNT = 20
DEFAULT_SAMPLE_COUNT = 20


def nearest_rank(values: Sequence[float], percentile: float) -> float:
    """按 nearest-rank 定义计算分位数，避免不同统计库插值规则漂移。"""
    if not values:
        raise ValueError("分位数样本不能为空")
    if not 0 < percentile <= 100:
        raise ValueError("percentile 必须位于 (0, 100]")
    ordered = sorted(float(value) for value in values)
    rank = math.ceil(percentile / 100 * len(ordered))
    return ordered[rank - 1]


def summarize_measurements(
    durations: Sequence[float],
    report: dict[str, Any],
    config: dict[str, Any],
    *,
    started_at: str,
    git_revision: str,
) -> dict[str, Any]:
    """生成不含本机路径的稳定基准摘要。"""
    if len(durations) < MIN_SAMPLE_COUNT:
        raise ValueError(f"P95/P99 基准至少需要 {MIN_SAMPLE_COUNT} 个真实样本")
    samples = [round(float(duration), 6) for duration in durations]
    warm_samples = samples[1:]
    return {
        "schema_version": "finance_backtest_latency_benchmark_v1",
        "started_at": started_at,
        "sample_count": len(samples),
        "method": "sequential_real_backtest_nearest_rank",
        "latency_seconds": {
            "cold_start": samples[0],
            "minimum": min(samples),
            "median": nearest_rank(samples, 50),
            "p95": nearest_rank(samples, 95),
            "p99": nearest_rank(samples, 99),
            "maximum": max(samples),
            "warm_p95": nearest_rank(warm_samples, 95),
            "warm_p99": nearest_rank(warm_samples, 99),
            "samples": samples,
        },
        "workload": {
            "codes": config.get("codes"),
            "start_date": config.get("start_date"),
            "end_date": config.get("end_date"),
            "source": config.get("source"),
            "interval": config.get("interval"),
            "engine": config.get("engine"),
        },
        "result_contract": {
            "schema_version": report.get("schema_version"),
            "engine_version": report.get("engine_version"),
            "data_source": report.get("data_source"),
            "benchmark_symbol": (report.get("benchmark") or {}).get("symbol"),
            "strategy_code_sha256": report.get("strategy_code_sha256"),
        },
        "environment": {
            "git_revision": git_revision,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
    }


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label}无法读取：{error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label}必须是 JSON 对象")
    return value


def _git_revision(repo_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def run_benchmark(
    template_dir: Path,
    sample_count: int,
    *,
    keep_runs: bool,
) -> dict[str, Any]:
    """顺序执行真实回测；任一样本失败即拒绝生成统计证据。"""
    if sample_count < MIN_SAMPLE_COUNT:
        raise ValueError(f"--runs 不得小于 {MIN_SAMPLE_COUNT}")
    template_dir = template_dir.resolve()
    config = _read_json(template_dir / "config.json", "基准 config.json")
    if not (template_dir / "code" / "signal_engine.py").is_file():
        raise ValueError("基准场景缺少 code/signal_engine.py")

    agent_root = Path(__file__).resolve().parents[1]
    repo_root = agent_root.parent
    run_root = agent_root / "runs" / f"finance-latency-{uuid.uuid4().hex}"
    started_at = datetime.now(UTC).isoformat()
    durations: list[float] = []
    contract_evidence: dict[str, Any] | None = None

    # 延迟导入保证 `--help` 和纯统计单测不加载完整回测依赖栈。
    sys.path.insert(0, str(agent_root))
    from src.tools.backtest_tool import run_backtest

    try:
        run_root.mkdir(parents=True, exist_ok=False)
        for index in range(sample_count):
            run_dir = run_root / f"sample-{index + 1:03d}"
            shutil.copytree(template_dir, run_dir)
            started = time.perf_counter()
            runner_output = io.StringIO()
            with contextlib.redirect_stdout(runner_output):
                raw = run_backtest(str(run_dir))
            elapsed = time.perf_counter() - started
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as error:
                raise RuntimeError(f"第 {index + 1} 次回测返回非 JSON 响应") from error
            if payload.get("status") != "ok" or not isinstance(payload.get("report"), dict):
                detail = str(
                    payload.get("error")
                    or payload.get("stderr")
                    or runner_output.getvalue()
                    or "未知失败"
                )
                raise RuntimeError(f"第 {index + 1} 次真实回测失败：{detail[:500]}")
            report = payload["report"]
            evidence = {
                "schema_version": report.get("schema_version"),
                "engine_version": report.get("engine_version"),
                "data_source": report.get("data_source"),
                "strategy_code_sha256": report.get("strategy_code_sha256"),
            }
            if contract_evidence is None:
                contract_evidence = evidence
            elif evidence != contract_evidence:
                raise RuntimeError("回测样本的引擎、数据源或策略指纹发生漂移")
            durations.append(elapsed)

        if contract_evidence is None:
            raise RuntimeError("没有完成任何真实回测样本")
        final_report = _read_json(
            run_root / f"sample-{sample_count:03d}" / "finance_backtest_report.json",
            "最终稳定回测报告",
        )
        return summarize_measurements(
            durations,
            final_report,
            config,
            started_at=started_at,
            git_revision=_git_revision(repo_root),
        )
    finally:
        if not keep_runs:
            shutil.rmtree(run_root, ignore_errors=True)


def _parse_args() -> argparse.Namespace:
    agent_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--template-dir",
        type=Path,
        default=agent_root / "benchmarks" / "finance_backtest_a_share",
        help="真实回测场景目录",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=DEFAULT_SAMPLE_COUNT,
        help=f"顺序样本数，不得少于 {MIN_SAMPLE_COUNT}",
    )
    parser.add_argument("--output", type=Path, help="可选：把 JSON 证据写入指定文件")
    parser.add_argument(
        "--keep-runs",
        action="store_true",
        help="保留每个真实回测目录供人工审计",
    )
    return parser.parse_args()


def main() -> int:
    """执行命令行基准并输出 JSON。"""
    args = _parse_args()
    try:
        summary = run_benchmark(args.template_dir, args.runs, keep_runs=args.keep_runs)
    except (ValueError, RuntimeError) as error:
        print(f"基准失败：{error}", file=sys.stderr)
        return 1
    encoded = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n", encoding="utf-8")
    print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
