"""The shared agent report format.

Every specialist hands Atlas the same structured report so Atlas can compare
them and the Auditor can score them later. Two rules apply to every report:

  1. A claim without a source can't be a reason for a trade.
  2. A stance without a wrong_if is sent back (rejected at submission).
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from atlas import models


class Stance(str, Enum):
    BUY = "BUY"
    ADD = "ADD"
    HOLD = "HOLD"
    TRIM = "TRIM"
    SELL = "SELL"
    NO_VIEW = "NO VIEW"


class Urgency(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


TRADE_STANCES = {Stance.BUY, Stance.ADD, Stance.TRIM, Stance.SELL}


class Evidence(BaseModel):
    """One claim and where it came from.

    `source` is a URL or a data-record reference such as "record:prices/123"
    or "record:positions/nonreg/NVDA.US". Leave it empty only if there is
    genuinely no source - and then the claim can't support a trade.
    """

    model_config = ConfigDict(frozen=True)

    claim: str = Field(min_length=1)
    source: str | None = None

    @property
    def sourced(self) -> bool:
        return bool(self.source and self.source.strip())


class ReportSentBack(ValueError):
    """Raised when a report breaks a format rule. The agent must fix and resubmit."""


class AgentReport(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    agent: str = Field(min_length=1, description="Who wrote it, e.g. value_research")
    as_of: datetime = Field(description="Date and time of the data it used")
    subject: str = Field(min_length=1, description='A holding (ticker), a goal id, or "portfolio"')
    finding: str = Field(min_length=1, description="One plain sentence")
    stance: Stance
    confidence: Decimal = Field(ge=0, le=1)
    urgency: Urgency
    evidence: list[Evidence] = Field(default_factory=list)
    wrong_if: str | None = Field(default=None, description="What would prove this call wrong")
    next_check: datetime = Field(description="When the agent will look again")
    id: int | None = None
    created_at: datetime | None = None

    @field_validator("as_of", "next_check")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("datetimes must be timezone-aware")
        return v

    @field_validator("finding")
    @classmethod
    def _one_sentence(cls, v: str) -> str:
        v = " ".join(v.split())
        if len(v) > 400:
            raise ValueError("finding must be one plain sentence (<= 400 chars)")
        return v

    @model_validator(mode="after")
    def _wrong_if_required(self) -> "AgentReport":
        if self.stance != Stance.NO_VIEW and not (self.wrong_if and self.wrong_if.strip()):
            raise ValueError(f"stance {self.stance.value} without a wrong_if is sent back")
        return self

    # --- rule 1: only sourced claims can support a trade -------------------
    def sourced_evidence(self) -> list[Evidence]:
        return [e for e in self.evidence if e.sourced]

    def unsourced_evidence(self) -> list[Evidence]:
        return [e for e in self.evidence if not e.sourced]

    @property
    def can_support_trade(self) -> bool:
        return self.stance in TRADE_STANCES and bool(self.sourced_evidence())

    def to_public(self) -> dict:
        """JSON-ready dict using the spec's field names (stance as 'NO VIEW', etc.)."""
        data = json.loads(self.model_dump_json())
        data["stance"] = self.stance.value
        data["urgency"] = self.urgency.value
        return data

    @classmethod
    def json_schema(cls) -> dict:
        """Schema for agents (e.g. Claude structured output) to fill in."""
        schema = cls.model_json_schema()
        for f in ("id", "created_at"):
            schema.get("properties", {}).pop(f, None)
        return schema


def _from_row(row: models.Report) -> AgentReport:
    return AgentReport(
        id=row.id,
        agent=row.agent,
        as_of=row.as_of,
        subject=row.subject,
        finding=row.finding,
        stance=Stance(row.stance),
        confidence=row.confidence,
        urgency=Urgency(row.urgency),
        evidence=[Evidence(**e) for e in row.evidence],
        wrong_if=row.wrong_if,
        next_check=row.next_check,
        created_at=row.created_at,
    )


class ReportDesk:
    """Where reports are filed and read. Validation happens on the way in."""

    def __init__(self, session: Session):
        self.session = session

    def submit(self, report: AgentReport | dict) -> AgentReport:
        try:
            report = report if isinstance(report, AgentReport) else AgentReport.model_validate(report)
        except ValueError as exc:  # pydantic.ValidationError is a ValueError
            raise ReportSentBack(str(exc)) from exc
        row = models.Report(
            agent=report.agent,
            as_of=report.as_of,
            subject=report.subject,
            finding=report.finding,
            stance=report.stance.value,
            confidence=report.confidence,
            urgency=report.urgency.value,
            evidence=[e.model_dump() for e in report.evidence],
            wrong_if=report.wrong_if,
            next_check=report.next_check,
        )
        self.session.add(row)
        self.session.flush()
        return _from_row(row)

    def get(self, report_id: int) -> AgentReport | None:
        row = self.session.get(models.Report, report_id)
        return _from_row(row) if row else None

    def recent(self, *, agent: str | None = None, subject: str | None = None, limit: int = 20) -> list[AgentReport]:
        q = select(models.Report).order_by(models.Report.created_at.desc(), models.Report.id.desc())
        if agent:
            q = q.where(models.Report.agent == agent)
        if subject:
            q = q.where(models.Report.subject == subject)
        return [_from_row(r) for r in self.session.scalars(q.limit(limit))]

    def latest_per_agent(self, subject: str | None = None) -> list[AgentReport]:
        """The newest report from each agent (optionally on one subject)."""
        seen: dict[str, AgentReport] = {}
        for rep in self.recent(subject=subject, limit=500):
            seen.setdefault(rep.agent, rep)
        return list(seen.values())


def dissent(reports: list[AgentReport]) -> list[AgentReport]:
    """Reports whose stance differs from the majority view (NO VIEW ignored).

    Atlas must never hide these.
    """
    stances = [r.stance for r in reports if r.stance != Stance.NO_VIEW]
    if len(stances) < 2:
        return []
    counts: dict[Stance, int] = {}
    for s in stances:
        counts[s] = counts.get(s, 0) + 1
    top = max(counts.values())
    majority = {s for s, c in counts.items() if c == top}
    if len(majority) > 1:  # a tie: everyone is "dissenting" from everyone
        return [r for r in reports if r.stance != Stance.NO_VIEW]
    return [r for r in reports if r.stance != Stance.NO_VIEW and r.stance not in majority]
