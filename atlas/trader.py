"""The Trader: carries out approved trades. Plain code, no AI judgment.

Flow:  propose -> Risk Officer check -> (blocked | proposed)
       proposed -> you approve (re-checked) -> approved -> executed at the broker

Safety: limit orders only, the kill switch, the loss limits (via the Risk
Officer), and it can't act on anything not approved or anything that changed
after approval (an approval is bound to a hash of the exact order).
Phase 1 routes orders to the PaperBroker only.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from atlas import models
from atlas.brokers.base import BrokerAdapter, BrokerError, OrderNotSupported, OrderRequest
from atlas.db import utcnow
from atlas.reports import ReportDesk
from atlas.risk import USER, RiskOfficer, RuleBook, TradeIntent


class TradeRefused(RuntimeError):
    pass


def order_hash(p: models.Proposal) -> str:
    parts = [p.account_id, p.ticker, p.side, format(Decimal(p.quantity), "f"),
             format(Decimal(p.limit_price), "f") if p.limit_price is not None else "", p.order_type]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


class Trader:
    actor = "trader"

    def __init__(self, session: Session, risk: RiskOfficer, rules: RuleBook, broker: BrokerAdapter):
        self.session = session
        self.risk = risk
        self.rules = rules
        self.broker = broker

    def _intent(self, p: models.Proposal) -> TradeIntent:
        return TradeIntent(p.account_id, p.ticker, p.side, p.quantity, p.limit_price, p.order_type)

    def propose(self, intent: TradeIntent, *, rationale: str = "", proposed_by: str = USER,
                report_ids: list[int] | None = None) -> models.Proposal:
        report_ids = report_ids or []
        self._check_reports(report_ids, proposed_by)
        decision = self.risk.check_trade(intent)
        p = models.Proposal(
            account_id=intent.account_id, ticker=intent.ticker, side=intent.side.upper(), quantity=intent.quantity,
            limit_price=intent.limit_price, order_type=intent.order_type.upper(), rationale=rationale,
            proposed_by=proposed_by, report_ids=report_ids,
            status="proposed" if decision.passed else "blocked", risk_result=decision.to_dict(),
        )
        self.session.add(p)
        self.session.flush()
        models.audit(self.session, proposed_by, "propose", proposal=p.id, status=p.status, risk=decision.summary())
        return p

    def _check_reports(self, report_ids: list[int], proposed_by: str) -> None:
        desk = ReportDesk(self.session)
        reports = [desk.get(i) for i in report_ids]
        if any(r is None for r in reports):
            raise TradeRefused("a cited report does not exist")
        weak = [r.id for r in reports if not r.can_support_trade]
        if weak:
            raise TradeRefused(f"reports {weak} have no sourced claims for a trade stance and can't justify a trade")
        if proposed_by != USER and not reports:
            raise TradeRefused("an agent's proposal must cite at least one report with sourced evidence")

    # -- multi-leg plans ---------------------------------------------------------
    def propose_plan(self, legs: list[tuple[TradeIntent, str]], *, proposed_by: str = USER,
                     report_ids: list[int] | None = None) -> list[models.Proposal]:
        """Propose several legs as one plan. legs = [(intent, rationale), ...], in execution order
        (sells first). The Risk Officer checks them as a sequence."""
        report_ids = report_ids or []
        self._check_reports(report_ids, proposed_by)
        group = f"plan-{uuid.uuid4().hex[:10]}"
        decisions = self.risk.check_plan([i for i, _ in legs])
        out = []
        for seq, ((intent, why), decision) in enumerate(zip(legs, decisions)):
            p = models.Proposal(
                account_id=intent.account_id, ticker=intent.ticker, side=intent.side.upper(),
                quantity=intent.quantity, limit_price=intent.limit_price, order_type=intent.order_type.upper(),
                rationale=why, proposed_by=proposed_by, report_ids=report_ids, group_id=group, sequence=seq,
                status="proposed" if decision.passed else "blocked", risk_result=decision.to_dict(),
            )
            self.session.add(p)
            out.append(p)
        self.session.flush()
        models.audit(self.session, proposed_by, "propose_plan", group=group, legs=[p.id for p in out],
                     blocked=[p.id for p in out if p.status == "blocked"])
        return out

    def plan(self, group_id: str) -> list[models.Proposal]:
        from sqlalchemy import select

        return list(self.session.scalars(select(models.Proposal).where(models.Proposal.group_id == group_id)
                                         .order_by(models.Proposal.sequence)))

    def approve_plan(self, group_id: str, actor: str = USER) -> list[models.Proposal]:
        """Approve every leg still awaiting approval, re-check them as a sequence, then execute in order.
        A leg that no longer passes is blocked and skipped; the others still run."""
        if actor != USER:
            raise TradeRefused("only you can approve a trade")
        legs = [p for p in self.plan(group_id) if p.status == "proposed"]
        if not legs:
            raise TradeRefused(f"plan {group_id} has nothing awaiting approval")
        decisions = self.risk.check_plan([self._intent(p) for p in legs])
        now = utcnow()
        for p, d in zip(legs, decisions):
            p.risk_result, p.decided_at = d.to_dict(), now
            if d.passed:
                p.status, p.approval_hash = "approved", order_hash(p)
            else:
                p.status = "blocked"
        models.audit(self.session, actor, "approve_plan", group=group_id,
                     approved=[p.id for p in legs if p.status == "approved"])
        self.session.flush()
        for p in legs:
            if p.status == "approved":
                try:
                    self.execute(p.id)
                except TradeRefused:
                    pass  # recorded on the proposal and in the audit log
        return self.plan(group_id)

    def approve(self, proposal_id: int, actor: str = USER, execute: bool = True) -> models.Proposal:
        if actor != USER:
            raise TradeRefused("only you can approve a trade")
        p = self._get(proposal_id)
        if p.status != "proposed":
            raise TradeRefused(f"proposal {p.id} is {p.status}, not awaiting approval")
        decision = self.risk.check_trade(self._intent(p))  # data may have moved since it was proposed
        p.risk_result = decision.to_dict()
        p.decided_at = utcnow()
        if not decision.passed:
            p.status = "blocked"
            models.audit(self.session, actor, "approve_blocked", proposal=p.id, risk=decision.summary())
            self.session.flush()
            return p
        p.status, p.approval_hash = "approved", order_hash(p)
        models.audit(self.session, actor, "approve", proposal=p.id, hash=p.approval_hash)
        self.session.flush()
        return self.execute(p.id) if execute else p

    def dismiss(self, proposal_id: int, actor: str = USER, reason: str = "") -> models.Proposal:
        p = self._get(proposal_id)
        if p.status not in {"proposed", "blocked", "approved"}:
            raise TradeRefused(f"proposal {p.id} is already {p.status}")
        p.status, p.decided_at = "dismissed", utcnow()
        models.audit(self.session, actor, "dismiss", proposal=p.id, reason=reason)
        self.session.flush()
        return p

    def execute(self, proposal_id: int) -> models.Proposal:
        p = self._get(proposal_id)
        if p.status != "approved":
            raise TradeRefused(f"proposal {p.id} is {p.status}; only approved proposals execute")
        if p.approval_hash != order_hash(p):
            p.status = "failed"
            models.audit(self.session, self.actor, "execute_refused", proposal=p.id, reason="changed after approval")
            raise TradeRefused("the order changed after approval; it needs a fresh approval")
        if self.rules.kill_switch().engaged:
            raise TradeRefused("kill switch is engaged; no trading")
        if not self.broker.can_trade:
            raise TradeRefused(f"broker '{self.broker.name}' can't place orders")
        account = self.session.get(models.Account, p.account_id)
        if account.broker != self.broker.name:
            raise TradeRefused(f"account {account.id} belongs to broker '{account.broker}', not '{self.broker.name}'")
        decision = self.risk.check_trade(self._intent(p))  # last look, right before sending
        if not decision.passed:
            p.status, p.risk_result = "blocked", decision.to_dict()
            models.audit(self.session, self.actor, "execute_blocked", proposal=p.id, risk=decision.summary())
            self.session.flush()
            return p
        try:
            order = self.broker.place_order(OrderRequest(p.account_id, p.ticker, p.side, p.quantity, p.limit_price,
                                                         p.order_type, proposal_id=p.id))
        except (BrokerError, OrderNotSupported) as exc:
            p.status = "failed"
            models.audit(self.session, self.actor, "execute_failed", proposal=p.id, error=str(exc))
            self.session.flush()
            raise TradeRefused(str(exc)) from exc
        p.status, p.order_id = "executed", order.id
        models.audit(self.session, self.actor, "execute", proposal=p.id, order=order.id, order_status=order.status)
        self.session.flush()
        return p

    def _get(self, proposal_id: int) -> models.Proposal:
        p = self.session.get(models.Proposal, proposal_id)
        if p is None:
            raise TradeRefused(f"no proposal {proposal_id}")
        return p
