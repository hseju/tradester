import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from atlas import models
from atlas.chat import ClaudeAtlas, OfflineAtlas, ToolBox
from atlas.chat.atlas import SYSTEM_PROMPT
from atlas.jobs import build_scheduler, job_daily_close, job_news, job_prices
from atlas.prices.base import NewsItem


# --- toolbox --------------------------------------------------------------
def test_every_tool_runs_and_is_read_only(seeded):
    names = [t["name"] for t in ToolBox.specs()]
    assert not any(w in n for n in names for w in ("place", "approve", "propose", "set_rule", "kill", "write"))
    args = {"check_trade": {"account_id": "paper-tfsa", "ticker": "XEQT.TO", "side": "BUY", "quantity": 1,
                            "limit_price": 36},
            "get_news": {"ticker": "NVDA"}, "get_quote": {"ticker": "nvda.us"}}
    with seeded.services() as s:
        before = (s.session.query(models.Position).count(), s.session.query(models.Proposal).count(),
                  s.session.query(models.Order).count())
        tb = ToolBox(s)
        for n in names:
            out = json.loads(tb.run(n, args.get(n, {})))
            assert isinstance(out, dict)
        after = (s.session.query(models.Position).count(), s.session.query(models.Proposal).count(),
                 s.session.query(models.Order).count())
    assert before == after


def test_overview_tool_content(seeded):
    with seeded.services() as s:
        ov = json.loads(ToolBox(s).run("get_portfolio_overview", {}))
        assert ov["net_worth"] == "88190.00" and ov["uses_sample_prices"] is True
        risk = json.loads(ToolBox(s).run("get_risk_status", {}))
        assert risk["current_breaches"][0]["subject"] == "NVDA.US"


# --- offline atlas -----------------------------------------------------------
def test_offline_atlas_answers_from_the_books(seeded):
    atlas = OfflineAtlas(seeded)
    brief = atlas.ask("how am I doing?")
    assert "$88,190" in brief and "NVDA.US is 13.6%" in brief and "SAMPLE" in brief
    assert "House down payment" in atlas.ask("how are my goals?")
    assert "US stocks: 55.0% vs 48.0% target  <- outside band" in atlas.ask("show my allocation")
    assert "Kill switch: off" in atlas.ask("any rule breaches?")


# --- claude atlas (fake client) ----------------------------------------------
class FakeMessages:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        return self.script.pop(0)


def block(**kw):
    return SimpleNamespace(**kw)


def test_claude_atlas_tool_loop(seeded):
    script = [
        SimpleNamespace(stop_reason="tool_use", content=[
            block(type="thinking", thinking="", signature="sig"),
            block(type="tool_use", id="t1", name="get_portfolio_overview", input={}),
            block(type="tool_use", id="t2", name="get_quote", input={"ticker": "NOPE.US"}),
            block(type="tool_use", id="t3", name="does_not_exist", input={}),
        ]),
        SimpleNamespace(stop_reason="end_turn", content=[block(type="text", text="Net worth is $88,190.")]),
    ]
    fake = FakeMessages(script)
    client = SimpleNamespace(beta=SimpleNamespace(messages=fake))
    atlas = ClaudeAtlas(seeded, client=client, model="claude-opus-5-5", effort="medium")
    assert atlas.ask("what's my net worth?") == "Net worth is $88,190."
    first, second = fake.calls
    assert first["system"] == SYSTEM_PROMPT and first["model"] == "claude-opus-5-5"
    assert first["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in first["betas"]
    assert first["output_config"] == {"effort": "medium"}
    results = second["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["t1", "t2", "t3"]  # all results in ONE user message
    assert json.loads(results[0]["content"])["net_worth"] == "88190.00"
    assert results[2]["is_error"] is True
    assert second["messages"][-2]["content"][0].type == "thinking"  # assistant content passed back unchanged


def test_claude_atlas_refusal_rolls_back_history(seeded):
    fake = FakeMessages([SimpleNamespace(stop_reason="refusal", content=[])])
    atlas = ClaudeAtlas(seeded, client=SimpleNamespace(beta=SimpleNamespace(messages=fake)))
    assert "can't help" in atlas.ask("hi")
    assert atlas.messages == []


# --- jobs ----------------------------------------------------------------------
def test_job_prices_files_breach_and_skips_when_closed(seeded, clock):
    out = job_prices(seeded)
    assert out["breach_reports"]  # NVDA over the cap
    assert out["advisor"]["reports"]  # US stocks past their band -> the Advisor runs straight away
    clock.t += timedelta(hours=10)
    assert job_prices(seeded) == {"skipped": "market closed"}


def test_job_news_marks_major_headlines(seeded, sample, clock):
    sample.get_news = lambda t, a, b: [NewsItem(t, clock() - timedelta(hours=1), "Royal Bank announces acquisition",
                                                "", "wire", "", "sample", f"{t}-1")] if t == "RY.TO" else []
    first = job_news(seeded)
    assert first["headlines_added"] == 1 and first["classified"] == 1 and first["reports"]
    assert job_news(seeded)["headlines_added"] == 0  # de-duplicated
    with seeded.services() as s:
        h = s.books.headlines("RY.TO")[0]
        assert h.is_major and h.relevant and h.classified_by == "keywords"


def test_daily_close_and_scheduler(seeded):
    out = job_daily_close(seeded)
    assert out["bookkeeper_report"]
    sched = build_scheduler(seeded, blocking=False)
    assert {j.id for j in sched.get_jobs()} == {"prices", "prices_close", "market_watch", "morning_scan",
                                                "daily_close", "advisor"}


# --- api -------------------------------------------------------------------------
@pytest.fixture
def api(seeded):
    from fastapi.testclient import TestClient

    from atlas.api import create_api

    return TestClient(create_api(seeded))


def test_api_reads(api):
    assert api.get("/health").json()["sample_prices"] is True
    assert api.get("/portfolio").json()["net_worth"] == "88190.00"
    r = api.post("/risk/check", json={"account_id": "paper-nonreg", "ticker": "NVDA.US", "side": "BUY",
                                      "quantity": 1, "limit_price": 190})
    assert r.json()["passed"] is False and r.json()["dry_run"] is True


def test_api_writes_need_token(api, monkeypatch):
    body = {"account_id": "paper-nonreg", "ticker": "NVDA.US", "side": "SELL", "quantity": 15, "limit_price": 189}
    monkeypatch.delenv("ATLAS_API_TOKEN", raising=False)
    assert api.post("/proposals", json=body).status_code == 403
    monkeypatch.setenv("ATLAS_API_TOKEN", "t0k")
    assert api.post("/proposals", json=body, headers={"X-Atlas-Token": "nope"}).status_code == 401
    pid = api.post("/proposals", json=body, headers={"X-Atlas-Token": "t0k"}).json()["id"]
    r = api.post(f"/proposals/{pid}/approve", headers={"X-Atlas-Token": "t0k"})
    assert r.json()["status"] == "executed"
    assert api.post("/kill-switch", json={"engaged": True}, headers={"X-Atlas-Token": "t0k"}).json()["engaged"]


def test_chat_endpoint_offline(api):
    r = api.post("/chat", json={"message": "what are my goals?"}).json()
    assert r["engine"] == "offline" and "House down payment" in r["reply"]


def test_claude_atlas_request_through_real_sdk(seeded):
    """The real anthropic SDK builds and parses the requests (HTTP mocked), incl. tool-result replay."""
    import anthropic
    import httpx2

    sent = []
    replies = [
        {"id": "m1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "tool_use",
         "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5},
         "content": [{"type": "tool_use", "id": "toolu_1", "name": "get_allocation", "input": {}}]},
        {"id": "m2", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": "end_turn",
         "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 5},
         "content": [{"type": "text", "text": "US stocks are 7 points over target."}]},
    ]

    def handler(req):
        sent.append((req.headers, json.loads(req.content)))
        return httpx2.Response(200, json=replies[len(sent) - 1])

    client = anthropic.Anthropic(api_key="test", max_retries=0,
                                 http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)))
    atlas = ClaudeAtlas(seeded, client=client)
    assert atlas.ask("am I on target?") == "US stocks are 7 points over target."
    headers, body = sent[0]
    assert "server-side-fallback-2026-07-01" in headers["anthropic-beta"]
    assert body["fallbacks"] == "default" and body["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"] == {"effort": "medium"} and body["max_tokens"] == 16000
    assert [t["name"] for t in body["tools"]] == [t["name"] for t in ToolBox.specs()]
    _, body2 = sent[1]
    assert body2["messages"][1]["content"][0]["type"] == "tool_use"
    assert body2["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"
