"""金融 swarm 投研产物契约测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.swarm.models import SwarmRun, SwarmTask, TaskStatus
from src.swarm.presets import build_run_from_preset
from src.swarm.research_report import publish_research_report
from src.swarm.store import swarm_runs_root
from src.swarm.task_store import TaskStore


def _report_payload() -> dict:
    return {
        "schema_version": "research_report_v1",
        "symbol": "600519.SH",
        "market": "CN",
        "display_name": "贵州茅台",
        "title": "贵州茅台投资委员会报告",
        "verdict": "bullish",
        "conviction": 4,
        "summary": "盈利韧性仍强，但估值限制仓位。",
        "body_md": "# 结论\n\n分批建仓，严格执行止损。",
        "findings": {
            "valuation": ["估值位于历史中枢上方"],
            "risks": ["需求恢复不及预期"],
            "catalysts": ["中报现金流改善"],
        },
        "data_as_of": "2026-07-18",
    }


def test_owner_data_root_controls_swarm_run_location(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIBE_TRADING_DATA_ROOT", str(tmp_path))

    assert swarm_runs_root() == tmp_path / "swarm" / "runs"


def test_owner_data_root_rejects_relative_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIBE_TRADING_DATA_ROOT", "relative-owner-root")

    with pytest.raises(ValueError, match="绝对路径"):
        swarm_runs_root()


def test_investment_committee_declares_research_report_contract() -> None:
    run = build_run_from_preset(
        "investment_committee",
        {"target": "600519.SH 贵州茅台", "market": "A股"},
    )

    assert run.result_contract == "research_report_v1"


def test_research_report_is_strictly_validated_and_atomically_published(
    tmp_path: Path,
) -> None:
    payload = _report_payload()

    report = publish_research_report(
        tmp_path,
        json.dumps(payload, ensure_ascii=False),
    )

    assert report.model_dump(mode="json") == payload
    assert json.loads((tmp_path / "review_report.json").read_text(encoding="utf-8")) == payload
    assert not (tmp_path / "review_report.tmp").exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"verdict": "buy"}, "verdict"),
        ({"data_as_of": "2026/07/18"}, "data_as_of"),
        ({"body_md": ""}, "body_md"),
        ({"run_dir": "/private/owner/run"}, "run_dir"),
    ],
)
def test_research_report_rejects_schema_mutation(
    tmp_path: Path,
    mutation: dict,
    message: str,
) -> None:
    payload = _report_payload()
    payload.update(mutation)

    with pytest.raises(ValueError, match=message):
        publish_research_report(
            tmp_path,
            json.dumps(payload, ensure_ascii=False),
        )

    assert not (tmp_path / "review_report.json").exists()


def test_research_report_rejects_invalid_json(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="JSON"):
        publish_research_report(tmp_path, '{"schema_version":')

    assert not (tmp_path / "review_report.json").exists()


def test_task_usage_round_trips_without_reading_summary(tmp_path: Path) -> None:
    store = TaskStore(tmp_path)
    store.save_task(
        SwarmTask(
            id="task-decision",
            agent_id="portfolio_manager",
            prompt_template="形成最终结论",
        )
    )

    updated = store.update_status(
        "task-decision",
        TaskStatus.completed,
        summary="这是只留在本机的原始 task 正文",
        worker_iterations=7,
        input_tokens=1234,
        output_tokens=321,
    )

    assert updated.input_tokens == 1234
    assert updated.output_tokens == 321
    persisted = json.loads((tmp_path / "tasks" / "task-task-decision.json").read_text(encoding="utf-8"))
    assert persisted["input_tokens"] == 1234
    assert persisted["output_tokens"] == 321


def test_legacy_swarm_run_defaults_new_contract_fields() -> None:
    run = SwarmRun.model_validate(
        {
            "id": "legacy-run",
            "preset_name": "legacy",
            "created_at": "2026-07-18T00:00:00+00:00",
        }
    )

    assert run.result_contract is None
