"""金融 swarm 的稳定投研报告产物契约。"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ResearchFindingsV1(BaseModel):
    """投研报告的结构化筛选要点。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    valuation: list[str]
    risks: list[str]
    catalysts: list[str]


class ResearchReportV1(BaseModel):
    """供 daemon 收割的严格 `research_report_v1`。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["research_report_v1"]
    symbol: str = Field(min_length=1, max_length=64)
    market: str = Field(min_length=1, max_length=32)
    display_name: str | None = Field(default=None, max_length=128)
    title: str = Field(min_length=1, max_length=256)
    verdict: Literal["bullish", "bearish", "neutral"]
    conviction: int | None = Field(default=None, ge=1, le=5)
    summary: str | None = Field(default=None, max_length=2000)
    body_md: str = Field(min_length=1)
    findings: ResearchFindingsV1
    data_as_of: date


def publish_research_report(run_dir: Path, raw_report: str) -> ResearchReportV1:
    """严格校验最终 task 输出，并原子发布 `review_report.json`。"""

    try:
        report = ResearchReportV1.model_validate_json(raw_report)
    except ValidationError as error:
        raise ValueError(f"投研报告 JSON/字段契约无效：{error}") from error

    target = run_dir / "review_report.json"
    temporary = run_dir / "review_report.tmp"
    temporary.write_text(
        report.model_dump_json(indent=2),
        encoding="utf-8",
    )
    temporary.replace(target)
    return report
