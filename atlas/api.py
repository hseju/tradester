"""HTTP API (FastAPI) and the phone web app it serves.

Access rules:
  - From this computer (loopback, not proxied), reads need no passcode.
  - From any other device - your phone over Wi-Fi, or anything arriving
    through a proxy such as Tailscale - every data endpoint needs the
    `X-Atlas-Token` header to match ATLAS_API_TOKEN (your passcode).
  - Anything that acts as you (approve, propose, dismiss, kill switch)
    always needs the passcode; with none configured those are disabled.
The app shell itself (HTML/JS/CSS/icons) holds no data and is served openly.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from decimal import Decimal

from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from atlas.agents import LIVE_AGENTS
from atlas.app import AtlasApp
from atlas.chat import ToolBox, make_atlas
from atlas.reports import Stance, dissent
from atlas.market import normalize_ticker
from atlas.risk import USER, RuleChangeRefused, TradeIntent
from atlas.trader import TradeRefused


class TradeIn(BaseModel):
    account_id: str
    ticker: str
    side: str = Field(pattern="^(BUY|SELL)$")
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal = Field(gt=0)
    rationale: str = ""


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = None


class KillIn(BaseModel):
    engaged: bool
    reason: str = ""


WEB_DIR = Path(__file__).parent / "web"
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
FORWARD_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "tailscale-user-login")

# The board as the spec describes it: who exists, and who is live yet.
BOARD = [
    ("bookkeeper", "BKP", "BOOKKEEPER"), ("risk_officer", "RSK", "RISK OFFICER"), ("advisor", "ADV", "ADVISOR"),
    ("market_watch", "MKT", "MARKET WATCH"), ("filings_research", "FIL", "FILINGS RESEARCH"),
    ("value_research", "VAL", "VALUE RESEARCH"), ("tax_accounts", "TAX", "TAX & ACCOUNTS"),
    ("savings_coach", "SAV", "SAVINGS COACH"), ("scout", "SCT", "SCOUT"), ("auditor", "AUD", "AUDITOR"),
]


def is_local(request: Request) -> bool:
    """A request typed on this computer, not relayed from elsewhere."""
    host = request.client.host if request.client else ""
    return host in LOOPBACK and not any(h in request.headers for h in FORWARD_HEADERS)


def token_ok(supplied: str | None) -> bool:
    expected = os.environ.get("ATLAS_API_TOKEN")
    return bool(expected and supplied and secrets.compare_digest(supplied, expected))


SHELL_HEADERS = {
    "Cache-Control": "no-cache",
    # Scripts only from this server: a headline can't smuggle code into the page.
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
                                "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
                                "font-src https://fonts.gstatic.com; base-uri 'none'; frame-ancestors 'none'; "
                                "form-action 'self'"),
}


def create_api(app: AtlasApp) -> FastAPI:
    api = FastAPI(title="Atlas", version="0.2.0")
    chats: dict[str, object] = {}

    def require_token(x_atlas_token: str | None = Header(default=None)) -> None:
        if not os.environ.get("ATLAS_API_TOKEN"):
            raise HTTPException(403, "write endpoints are disabled: set ATLAS_API_TOKEN")
        if not token_ok(x_atlas_token):
            raise HTTPException(401, "bad or missing X-Atlas-Token")

    def require_read(request: Request, x_atlas_token: str | None = Header(default=None)) -> None:
        if is_local(request) or token_ok(x_atlas_token):
            return
        if not os.environ.get("ATLAS_API_TOKEN"):
            raise HTTPException(403, "set ATLAS_API_TOKEN (run `atlas serve --phone`) to use Atlas from another device")
        raise HTTPException(401, "passcode required")

    def tool(name: str, **args):
        with app.services() as s:
            return json.loads(ToolBox(s).run(name, args))

    @api.middleware("http")
    async def no_store(request: Request, call_next):
        resp = await call_next(request)
        if not request.url.path.startswith("/static"):
            resp.headers.setdefault("Cache-Control", "no-store")  # financial data: never cached by the phone
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    @api.get("/health")
    def health():
        return {"ok": True, "sample_prices": app.prices.is_sample}

    @api.get("/portfolio", dependencies=[Depends(require_read)])
    def portfolio(book: str = "real"):
        return tool("get_portfolio_overview", book=book)

    @api.get("/holdings", dependencies=[Depends(require_read)])
    def holdings(book: str = "real"):
        return tool("get_holdings", book=book)

    @api.get("/allocation", dependencies=[Depends(require_read)])
    def allocation(book: str = "real"):
        return tool("get_allocation", book=book)

    @api.get("/goals", dependencies=[Depends(require_read)])
    def goals():
        return tool("get_goals")

    @api.get("/risk", dependencies=[Depends(require_read)])
    def risk(book: str = "real"):
        return tool("get_risk_status", book=book)

    @api.get("/reports", dependencies=[Depends(require_read)])
    def reports(subject: str | None = None, agent: str | None = None, limit: int = 20):
        args = {"limit": limit} | ({"subject": subject} if subject else {}) | ({"agent": agent} if agent else {})
        return tool("get_reports", **args)

    @api.get("/orders", dependencies=[Depends(require_read)])
    def orders(limit: int = 20):
        return tool("get_orders", limit=limit)

    @api.get("/events", dependencies=[Depends(require_read)])
    def events(days: int = 7):
        return tool("get_events", days=days)

    @api.get("/plans", dependencies=[Depends(require_read)])
    def plans(status: str = "open"):
        return tool("get_plans", status=status)

    @api.post("/risk/check", dependencies=[Depends(require_read)])
    def risk_check(t: TradeIn):
        return tool("check_trade", account_id=t.account_id, ticker=t.ticker, side=t.side,
                    quantity=float(t.quantity), limit_price=float(t.limit_price))

    @api.post("/chat", dependencies=[Depends(require_read)])
    def chat(c: ChatIn):
        sid = c.session_id if c.session_id in chats else uuid.uuid4().hex
        atlas = chats.setdefault(sid, make_atlas(app))
        return {"session_id": sid, "engine": atlas.name, "reply": atlas.ask(c.message)}

    @api.post("/proposals", dependencies=[Depends(require_token)])
    def propose(t: TradeIn):
        with app.services() as s:
            try:
                p = s.trader.propose(TradeIntent(t.account_id, normalize_ticker(t.ticker), t.side, t.quantity,
                                                 t.limit_price), rationale=t.rationale)
            except TradeRefused as exc:
                raise HTTPException(422, str(exc)) from None
            return {"id": p.id, "status": p.status, "risk": p.risk_result}

    @api.post("/proposals/{pid}/approve", dependencies=[Depends(require_token)])
    def approve(pid: int):
        with app.services() as s:
            try:
                p = s.trader.approve(pid, actor=USER)
            except TradeRefused as exc:
                raise HTTPException(409, str(exc)) from None
            return {"id": p.id, "status": p.status, "order_id": p.order_id, "risk": p.risk_result}

    @api.post("/proposals/{pid}/dismiss", dependencies=[Depends(require_token)])
    def dismiss(pid: int):
        with app.services() as s:
            try:
                p = s.trader.dismiss(pid, actor=USER)
            except TradeRefused as exc:
                raise HTTPException(409, str(exc)) from None
            return {"id": p.id, "status": p.status}

    @api.post("/plans/{group_id}/approve", dependencies=[Depends(require_token)])
    def approve_plan(group_id: str):
        with app.services() as s:
            try:
                legs = s.trader.approve_plan(group_id, actor=USER)
            except TradeRefused as exc:
                raise HTTPException(409, str(exc)) from None
            return {"plan_id": group_id, "legs": [{"id": p.id, "status": p.status, "order_id": p.order_id}
                                                  for p in legs]}

    @api.post("/plans/{group_id}/dismiss", dependencies=[Depends(require_token)])
    def dismiss_plan(group_id: str):
        with app.services() as s:
            legs = s.trader.plan(group_id)
            if not legs:
                raise HTTPException(404, "no such plan")
            for p in legs:
                if p.status in {"proposed", "blocked", "approved"}:
                    s.trader.dismiss(p.id, actor=USER, reason="stood down from the phone")
            return {"plan_id": group_id, "legs": [{"id": p.id, "status": p.status} for p in legs]}

    # -- phone app: aggregate reads ---------------------------------------------------
    @api.get("/session", dependencies=[Depends(require_read)])
    def session_info(request: Request):
        return {"ok": True, "local": is_local(request), "sample_prices": app.prices.is_sample,
                "chat_engine": "claude" if app.settings.anthropic_api_key else "offline",
                "trading": app.settings.trading_broker, "can_act": bool(os.environ.get("ATLAS_API_TOKEN"))}

    @api.get("/home", dependencies=[Depends(require_read)])
    def home():
        with app.services() as s:
            tb = ToolBox(s)
            run = lambda name, **a: json.loads(tb.run(name, a))  # noqa: E731
            return {
                "overview": run("get_portfolio_overview"), "allocation": run("get_allocation"),
                "risk": run("get_risk_status"), "holdings": run("get_holdings"), "goals": run("get_goals"),
                "plans": run("get_plans"), "events": run("get_events", days=7),
                "history": run("get_net_worth_history", days=30),
                "headlines": [{"ticker": h.ticker, "headline": h.headline, "major": h.is_major,
                               "published_at": h.published_at.isoformat()}
                              for h in s.books.headlines(limit=30) if h.relevant is not False][:8],
                "sample_prices": app.prices.is_sample, "trading": app.settings.trading_broker,
            }

    @api.get("/plans/{group_id}", dependencies=[Depends(require_read)])
    def plan_detail(group_id: str):
        with app.services() as s:
            legs = s.trader.plan(group_id)
            if not legs:
                raise HTTPException(404, "no such plan")
            reports = [s.desk.get(i) for i in legs[0].report_ids]

            def cad(p):
                inst = s.books.instrument(p.ticker)
                fx, _ = s.books.fx_to_cad(inst.currency if inst else "CAD")
                return str((p.quantity * p.limit_price * (fx or 1)).quantize(Decimal("0.01"))) if p.limit_price else None
            return {
                "plan_id": group_id, "proposed_by": legs[0].proposed_by, "created_at": legs[0].created_at.isoformat(),
                "legs": [{"id": p.id, "seq": p.sequence, "account": p.account_id, "side": p.side, "ticker": p.ticker,
                          "quantity": str(p.quantity), "limit_price": str(p.limit_price), "status": p.status,
                          "rationale": p.rationale, "order_id": p.order_id,
                          "est_value_cad": cad(p),
                          "checks": p.risk_result.get("checks", [])} for p in legs],
                "reports": [r.to_public() for r in reports if r],
            }

    @api.get("/board", dependencies=[Depends(require_read)])
    def board(subject: str | None = None):
        with app.services() as s:
            recent = s.desk.recent(limit=300)
            if subject is None:  # focus on the newest trade call, else the portfolio
                call = next((r for r in recent if r.stance not in (Stance.NO_VIEW, Stance.HOLD)
                             and r.subject != "portfolio"), None)
                subject = call.subject if call else "portfolio"
            on_subject = s.desk.latest_per_agent(subject=subject)
            by_agent = {r.agent: r for r in on_subject}
            latest_any: dict = {}
            for r in recent:
                latest_any.setdefault(r.agent, r)
            agents = []
            for key, code, role in BOARD:
                r = by_agent.get(key)
                last = latest_any.get(key)
                agents.append({"agent": key, "code": code, "role": role, "live": key in LIVE_AGENTS,
                               "report": r.to_public() if r else None,
                               "last_report_at": last.as_of.isoformat() if last else None})
            votes: dict[str, int] = {}
            for r in on_subject:
                votes[r.stance.value] = votes.get(r.stance.value, 0) + 1
            subjects = list(dict.fromkeys(r.subject for r in recent))[:12]
            return {"subject": subject, "subjects": subjects, "agents": agents, "votes": votes,
                    "dissent": [r.agent for r in dissent(on_subject)]}

    @api.get("/rules", dependencies=[Depends(require_read)])
    def rules():
        return tool("get_risk_status")

    # -- phone app: the shell ----------------------------------------------------------
    if WEB_DIR.is_dir():
        api.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

        @api.get("/", include_in_schema=False)
        def index():
            return FileResponse(WEB_DIR / "index.html", headers=SHELL_HEADERS)

        @api.get("/manifest.webmanifest", include_in_schema=False)
        def manifest():
            return FileResponse(WEB_DIR / "manifest.webmanifest", media_type="application/manifest+json")

        @api.get("/sw.js", include_in_schema=False)
        def service_worker():
            return FileResponse(WEB_DIR / "sw.js", media_type="text/javascript",
                                headers={"Cache-Control": "no-cache"})

    @api.post("/kill-switch", dependencies=[Depends(require_token)])
    def kill(k: KillIn):
        with app.services() as s:
            try:
                ks = (s.rules.engage_kill_switch(USER, k.reason or "engaged via API") if k.engaged
                      else s.rules.release_kill_switch(USER, k.reason))
            except RuleChangeRefused as exc:
                raise HTTPException(409, str(exc)) from None
            return {"engaged": ks.engaged, "reason": ks.reason}

    return api
