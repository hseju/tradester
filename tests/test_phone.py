"""The phone app: passcode rules, the app shell, and the phone endpoints."""

import pytest
from fastapi.testclient import TestClient

from atlas.agents import Advisor
from atlas.api import create_api


@pytest.fixture
def api(seeded, monkeypatch):
    monkeypatch.setenv("ATLAS_API_TOKEN", "pass-123")
    with seeded.services() as s:
        Advisor(s).run()
    return TestClient(create_api(seeded))


def remote(api):
    """A client that looks like another device on the network (the phone)."""
    return TestClient(api.app, client=("192.168.1.50", 50000))


def test_phone_needs_the_passcode_for_every_data_endpoint(api):
    phone = remote(api)
    for path in ("/home", "/portfolio", "/board", "/plans", "/session", "/risk", "/events"):
        assert phone.get(path).status_code == 401, path
        assert phone.get(path, headers={"X-Atlas-Token": "wrong"}).status_code == 401, path
        assert phone.get(path, headers={"X-Atlas-Token": "pass-123"}).status_code == 200, path
    assert phone.post("/chat", json={"message": "hi"}).status_code == 401


def test_proxied_requests_count_as_remote(api):
    """Tailscale (or any proxy) relays from 127.0.0.1; that must not skip the passcode."""
    assert api.get("/home").status_code == 200  # typed on this computer
    assert api.get("/home", headers={"X-Forwarded-For": "100.64.0.7"}).status_code == 401
    assert api.get("/home", headers={"Tailscale-User-Login": "me@example.com"}).status_code == 401


def test_without_a_passcode_configured_the_phone_is_turned_away(api, monkeypatch):
    monkeypatch.delenv("ATLAS_API_TOKEN")
    r = remote(api).get("/home")
    assert r.status_code == 403 and "atlas serve --phone" in r.json()["detail"]


def test_app_shell_is_served_with_a_strict_policy(api):
    r = remote(api).get("/")
    assert r.status_code == 200 and "<main id=\"app\"" in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    assert remote(api).get("/manifest.webmanifest").json()["display"] == "standalone"
    assert remote(api).get("/static/app.js").status_code == 200
    assert remote(api).get("/sw.js").status_code == 200
    assert api.get("/home").headers["cache-control"] == "no-store"  # finances are never cached


def test_home_board_and_decision_card(api):
    h = api.get("/home").json()
    assert h["overview"]["net_worth"] == "88190.00" and h["plans"]["plans"]
    gid = h["plans"]["plans"][0]["plan_id"]
    d = api.get(f"/plans/{gid}").json()
    leg = d["legs"][0]
    assert leg["ticker"] == "NVDA.US"
    # 14 x 189.52 USD x 1.40 = CAD value, not the USD number
    assert leg["est_value_cad"] == "3714.59"
    assert {c["rule"] for c in leg["checks"]} >= {"kill_switch", "max_single_stock_pct"}
    # The plan cites the drift call and the NVDA breach - not the goal checks.
    assert {r["subject"] for r in d["reports"]} <= {"portfolio", "NVDA.US"}
    assert not any(r["subject"] in {"house", "retirement"} for r in d["reports"])
    b = api.get("/board?subject=NVDA.US").json()
    stances = {a["agent"]: (a["report"] or {}).get("stance") for a in b["agents"]}
    assert stances["advisor"] == "TRIM" and stances["risk_officer"] == "TRIM"
    assert b["votes"] == {"TRIM": 2} and b["dissent"] == []
    assert sum(a["live"] for a in b["agents"]) == 4 and len(b["agents"]) == 10


def test_stand_down_from_the_phone(api):
    gid = api.get("/plans").json()["plans"][0]["plan_id"]
    phone = remote(api)
    assert phone.post(f"/plans/{gid}/dismiss").status_code == 401
    r = phone.post(f"/plans/{gid}/dismiss", headers={"X-Atlas-Token": "pass-123"}).json()
    assert {leg["status"] for leg in r["legs"]} == {"dismissed"}
    assert api.get("/plans").json()["plans"] == []
