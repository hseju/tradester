from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from atlas.reports import AgentReport, Evidence, ReportDesk, ReportSentBack, Stance, Urgency, dissent

NOW = datetime(2026, 11, 24, 15, tzinfo=timezone.utc)


def rep(**kw) -> dict:
    base = dict(agent="value_research", as_of=NOW, subject="NVDA.US", finding="Price is inside fair value.",
                stance="HOLD", confidence="0.6", urgency="low",
                evidence=[{"claim": "P/E 31 vs 5y median 38", "source": "record:fundamentals/NVDA.US"}],
                wrong_if="Next quarter's margin falls below 60%.", next_check=NOW + timedelta(days=7))
    base.update(kw)
    return base


def test_stance_without_wrong_if_is_sent_back(app):
    with app.services() as s:
        with pytest.raises(ReportSentBack, match="wrong_if"):
            ReportDesk(s.session).submit(rep(wrong_if=None))
        with pytest.raises(ReportSentBack):
            ReportDesk(s.session).submit(rep(wrong_if="   "))


def test_no_view_needs_no_wrong_if():
    r = AgentReport.model_validate(rep(stance="NO VIEW", wrong_if=None))
    assert r.stance is Stance.NO_VIEW and not r.can_support_trade


def test_unsourced_claims_cannot_support_a_trade():
    r = AgentReport.model_validate(rep(stance="TRIM", evidence=[{"claim": "Feels toppy"}]))
    assert r.unsourced_evidence() and not r.sourced_evidence()
    assert not r.can_support_trade
    r2 = AgentReport.model_validate(rep(stance="TRIM"))
    assert r2.can_support_trade


def test_field_validation():
    with pytest.raises(ValueError):
        AgentReport.model_validate(rep(confidence="1.2"))
    with pytest.raises(ValueError):
        AgentReport.model_validate(rep(as_of=datetime(2026, 11, 24)))  # naive
    with pytest.raises(ValueError):
        AgentReport.model_validate(rep(stance="MAYBE"))


def test_round_trip_and_public_format(app):
    with app.services() as s:
        desk = ReportDesk(s.session)
        saved = desk.submit(rep())
        got = desk.get(saved.id)
        assert got.evidence == [Evidence(claim="P/E 31 vs 5y median 38", source="record:fundamentals/NVDA.US")]
        assert got.as_of == NOW and got.confidence == Decimal("0.6")
        pub = desk.submit(rep(stance="NO VIEW", agent="filings_research")).to_public()
        assert pub["stance"] == "NO VIEW" and pub["urgency"] == "low"
        assert set(pub) >= {"agent", "as_of", "subject", "finding", "stance", "confidence", "urgency",
                            "evidence", "wrong_if", "next_check"}
        assert {r.agent for r in desk.latest_per_agent("NVDA.US")} == {"value_research", "filings_research"}


def test_dissent_is_the_minority_stance():
    mk = lambda agent, stance: AgentReport.model_validate(rep(agent=agent, stance=stance))  # noqa: E731
    board = [mk("advisor", "TRIM"), mk("risk_officer", "TRIM"), mk("market_watch", "TRIM"),
             mk("tax", "TRIM"), mk("value_research", "HOLD"),
             AgentReport.model_validate(rep(agent="filings", stance="NO VIEW", wrong_if=None))]
    assert [r.agent for r in dissent(board)] == ["value_research"]
    assert dissent(board[:1]) == []


def test_schema_has_spec_fields():
    props = AgentReport.json_schema()["properties"]
    assert {"agent", "as_of", "subject", "finding", "stance", "confidence", "urgency", "evidence", "wrong_if",
            "next_check"} <= set(props)
    assert "id" not in props
    assert Urgency.HIGH.value == "high"
