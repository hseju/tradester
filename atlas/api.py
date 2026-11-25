"""Local HTTP API (FastAPI) - the backend the phone app will talk to in phase 4.

Read endpoints are open on localhost. Anything that acts as you (approve,
propose, dismiss, kill switch) requires the `X-Atlas-Token` header to match
ATLAS_API_TOKEN; with no token configured those endpoints are disabled.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from decimal import Decimal

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from atlas.app import AtlasApp
from atlas.chat import ToolBox, make_atlas
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


def create_api(app: AtlasApp) -> FastAPI:
    api = FastAPI(title="Atlas", version="0.1.0")
    chats: dict[str, object] = {}

    def require_token(x_atlas_token: str | None = Header(default=None)) -> None:
        expected = os.environ.get("ATLAS_API_TOKEN")
        if not expected:
            raise HTTPException(403, "write endpoints are disabled: set ATLAS_API_TOKEN")
        if not x_atlas_token or not secrets.compare_digest(x_atlas_token, expected):
            raise HTTPException(401, "bad or missing X-Atlas-Token")

    def tool(name: str, **args):
        with app.services() as s:
            return json.loads(ToolBox(s).run(name, args))

    @api.get("/health")
    def health():
        return {"ok": True, "sample_prices": app.prices.is_sample}

    @api.get("/portfolio")
    def portfolio(book: str = "real"):
        return tool("get_portfolio_overview", book=book)

    @api.get("/holdings")
    def holdings(book: str = "real"):
        return tool("get_holdings", book=book)

    @api.get("/allocation")
    def allocation(book: str = "real"):
        return tool("get_allocation", book=book)

    @api.get("/goals")
    def goals():
        return tool("get_goals")

    @api.get("/risk")
    def risk(book: str = "real"):
        return tool("get_risk_status", book=book)

    @api.get("/reports")
    def reports(subject: str | None = None, agent: str | None = None, limit: int = 20):
        args = {"limit": limit} | ({"subject": subject} if subject else {}) | ({"agent": agent} if agent else {})
        return tool("get_reports", **args)

    @api.get("/orders")
    def orders(limit: int = 20):
        return tool("get_orders", limit=limit)

    @api.get("/events")
    def events(days: int = 7):
        return tool("get_events", days=days)

    @api.get("/plans")
    def plans(status: str = "open"):
        return tool("get_plans", status=status)

    @api.post("/risk/check")
    def risk_check(t: TradeIn):
        return tool("check_trade", account_id=t.account_id, ticker=t.ticker, side=t.side,
                    quantity=float(t.quantity), limit_price=float(t.limit_price))

    @api.post("/chat")
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
