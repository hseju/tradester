"""The Advisor: guards the plan. Is your money still set up to reach your goals?

  - Drift alerts: any part of the mix outside its band ("US stocks are 7 points over target").
  - Goal checks: projected value at the goal date from your contributions and
    the return assumptions you set (`atlas assume`), the return the goal
    actually needs, and a flag when a near-term goal sits mostly in stocks.
  - Rebalance plans: rule breaches first (e.g. a stock over the cap), then
    drift. Legs are proposed to the Trader in the paper book and wait for
    your approval; the Risk Officer checks them as a sequence.

It can propose; it can't approve, and it can't change your targets or assumptions.
Plans prefer registered accounts for sells (no tax), whole shares, a minimum
trade size, and never buy stocks inside an account tied to a goal less than
three years away.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import select

from atlas import models
from atlas.app import Services
from atlas.bookkeeper import EQUITY_BUCKETS, ZERO, BucketView, GoalView, PortfolioView, money, pct
from atlas.market import ET
from atlas.reports import AgentReport, Evidence, Stance, Urgency
from atlas.risk import TradeIntent

AGENT = "advisor"
REGISTERED = {"TFSA", "RRSP", "FHSA", "RESP", "LIRA", "RRIF"}
# Instrument to buy for a bucket, by the cash currency available in the account.
PREFERRED = {
    ("ca_equity", "CAD"): "XIC.TO",
    ("us_equity", "CAD"): "VFV.TO",
    ("us_equity", "USD"): "VOO.US",
    ("intl_equity", "CAD"): "XEF.TO",
    ("fixed_income", "CAD"): "ZAG.TO",
}
CUSHION = Decimal("0.0025")  # limit price 0.25% through the last price, so plan legs fill
CAP_BUFFER = Decimal("0.5")  # trim a breaching stock to half a point under the cap
SHORT_HORIZON_MONTHS = 36


@dataclass
class Leg:
    intent: TradeIntent
    rationale: str
    value_cad: Decimal


@dataclass
class PlanDraft:
    legs: list[Leg] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class GoalCheck:
    goal: GoalView
    expected_return_pct: Decimal
    projected: Decimal
    on_pace: bool
    required_return_pct: Decimal | None
    needed_monthly: Decimal | None
    equity_pct: Decimal


def usd0(d: Decimal) -> str:
    """Whole dollars for estimates and projections."""
    return f"${d:,.0f}"


def project(pv: float, monthly: float, annual_return: float, months: int) -> float:
    r = (1 + annual_return) ** (1 / 12) - 1
    if months <= 0:
        return pv
    if abs(r) < 1e-12:
        return pv + monthly * months
    g = (1 + r) ** months
    return pv * g + monthly * (g - 1) / r


def required_return(pv: float, monthly: float, target: float, months: int) -> float | None:
    """Annual return that makes the projection hit the target, or None if even 30%/yr won't."""
    lo, hi = -0.5, 0.30
    if project(pv, monthly, hi, months) < target:
        return None
    if project(pv, monthly, lo, months) >= target:
        return lo
    for _ in range(80):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if project(pv, monthly, mid, months) < target else (lo, mid)
    return hi


def needed_monthly(pv: float, target: float, annual_return: float, months: int) -> float | None:
    if months <= 0:
        return None
    r = (1 + annual_return) ** (1 / 12) - 1
    gap = target - pv * (1 + r) ** months
    if gap <= 0:
        return 0.0
    return gap / months if abs(r) < 1e-12 else gap * r / ((1 + r) ** months - 1)


class Advisor:
    agent = AGENT

    def __init__(self, s: Services):
        self.s = s
        self.books = s.books

    # -- helpers ------------------------------------------------------------------
    def _today(self):
        return self.books.now().astimezone(ET).date()

    def _today_reports(self, subject: str) -> list[AgentReport]:
        return [r for r in self.s.desk.recent(agent=AGENT, subject=subject, limit=20)
                if r.as_of.astimezone(ET).date() == self._today()]

    def _file(self, report: AgentReport, key=None) -> AgentReport | None:
        """File unless an equivalent report went in today. `key(report)` says what counts as the same
        call (e.g. same stance on the same buckets), so price wiggles don't refile it every few minutes."""
        key = key or (lambda r: (r.stance, r.finding))
        if any(key(r) == key(report) for r in self._today_reports(report.subject)):
            return None
        return self.s.desk.submit(report)

    # -- drift ----------------------------------------------------------------------
    def drift_report(self, view: PortfolioView, alloc: list[BucketView]) -> AgentReport | None:
        now = self.books.now()
        over = [b for b in alloc if b.outside_band and b.drift_pct > 0]
        under = [b for b in alloc if b.outside_band and b.drift_pct < 0]
        evidence = [Evidence(claim=f"{b.label} {b.pct:.1f}% vs {b.target_pct:.1f}% target (band ±{b.band_pct:.1f})",
                             source=f"record:target_allocation/{b.bucket}") for b in over + under]
        evidence.append(Evidence(claim=f"Portfolio total ${money(view.total_cad):,} CAD", source="record:portfolio/real"))
        if not over and not under:
            finding = "Your mix is within its bands; no rebalance needed."
            stance, urgency, wrong_if = Stance.HOLD, Urgency.LOW, "A bucket drifts past its band."
        else:
            parts = [f"{b.label} {abs(b.drift_pct):.1f} points {'over' if b.drift_pct > 0 else 'under'} target "
                     f"({b.pct:.1f}% vs {b.target_pct:.1f}%)" for b in over + under]
            finding = "; ".join(parts) + ". A rebalance restores your plan."
            stance = Stance.TRIM if over else Stance.ADD
            urgency, wrong_if = Urgency.MEDIUM, "You decide the target mix itself should change, or prices move it back inside the bands."
        # One drift call a day per stance; a new one only if the picture changes (e.g. drift -> back in band).
        if any(r.stance == stance and not r.finding.startswith("Proposed") for r in self._today_reports("portfolio")):
            return None
        return self._file(AgentReport(agent=AGENT, as_of=now, subject="portfolio", finding=finding, stance=stance,
                                      confidence=Decimal("0.8"), urgency=urgency, evidence=evidence,
                                      wrong_if=wrong_if, next_check=now + timedelta(days=1)))

    # -- goals ------------------------------------------------------------------------
    def check_goal(self, g: GoalView, view: PortfolioView) -> GoalCheck:
        er = self.books.assumptions()["expected_return_pct"]
        alloc = self.books.allocation(view, account_ids=g.account_ids)
        exp = sum((b.pct * er.get(b.bucket, ZERO) for b in alloc), ZERO) / 100 if alloc else ZERO
        equity = sum((b.pct for b in alloc if b.bucket in EQUITY_BUCKETS), ZERO)
        pv, c, n, tgt = float(g.current_cad), float(g.monthly_contribution), g.months_left, float(g.target_amount)
        projected = project(pv, c, float(exp) / 100, n)
        req = required_return(pv, c, tgt, n)
        need = needed_monthly(pv, tgt, float(exp) / 100, n)
        return GoalCheck(g, exp, Decimal(str(round(projected, 2))), projected >= tgt,
                         None if req is None else Decimal(str(round(req * 100, 2))),
                         None if need is None else Decimal(str(round(need, 2))), equity)

    def goal_reports(self, view: PortfolioView) -> list[AgentReport]:
        now = self.books.now()
        out = []
        for g in self.books.goals(view):
            gc = self.check_goal(g, view)
            when = f"{g.target_date:%b %Y}"
            contrib = f"{usd0(g.monthly_contribution)}/month" if g.monthly_contribution else "no monthly contribution set"
            if gc.on_pace:
                finding = (f"{g.name} is on pace: about {usd0(gc.projected)} by {when} vs {usd0(g.target_amount)} "
                           f"target, with {contrib} at {gc.expected_return_pct:.1f}%/yr")
                stance = Stance.HOLD
                wrong_if = (f"Returns average below {gc.required_return_pct:.1f}% a year until {when}."
                            if gc.required_return_pct is not None else "Contributions stop.")
            else:
                more = (f"; about {usd0(gc.needed_monthly)}/month would close the gap"
                        if gc.needed_monthly is not None else "")
                finding = (f"{g.name} is behind: about {usd0(gc.projected)} by {when} vs {usd0(g.target_amount)} "
                           f"target, with {contrib} at {gc.expected_return_pct:.1f}%/yr{more}")
                stance = Stance.ADD
                wrong_if = (f"Returns average above {gc.required_return_pct:.1f}% a year, which closes the gap on its own."
                            if gc.required_return_pct is not None else
                            "The target or date changes; returns alone can't close this gap.")
            short = g.months_left < SHORT_HORIZON_MONTHS
            if short and gc.equity_pct > 40:
                finding += f"; it is {gc.equity_pct:.0f}% stocks with {g.months_left} months to go, so a bad year could set it back"
            finding += "."
            evidence = [
                Evidence(claim=f"{g.name}: ${money(g.current_cad):,} now in {', '.join(g.account_ids)}",
                         source=f"record:goals/{g.id}"),
                Evidence(claim=f"Expected return {gc.expected_return_pct:.2f}%/yr from your assumptions and this mix",
                         source="record:system_flags/advisor_assumptions"),
            ]
            if self._today_reports(g.id):
                continue  # goal pacing is a daily call; intraday prices don't change it
            out.append(self._file(AgentReport(
                agent=AGENT, as_of=now, subject=g.id, finding=finding, stance=stance, confidence=Decimal("0.6"),
                urgency=Urgency.MEDIUM if (not gc.on_pace or (short and gc.equity_pct > 40)) else Urgency.LOW,
                evidence=evidence, wrong_if=wrong_if, next_check=now + timedelta(days=30))))
        return [r for r in out if r]

    # -- rebalance plan ------------------------------------------------------------------
    def build_plan(self, book: str = models.BOOK_PAPER) -> PlanDraft:
        draft = PlanDraft()
        view = self.books.portfolio(book)
        if not view.total_cad:
            return draft
        total = view.total_cad
        min_trade = self.books.assumptions()["min_trade_cad"]
        alloc = {b.bucket: b for b in self.books.allocation(view)}
        bv = {b: a.value_cad for b, a in alloc.items()}
        cash: dict[tuple[str, str], Decimal] = {}
        for c in view.cash:
            cash[(c.account_id, c.currency)] = cash.get((c.account_id, c.currency), ZERO) + c.amount
        qty = {(h.account_id, h.ticker): h.quantity for h in view.holdings}
        accounts = {a.id: a for a in self.books.accounts(book)}
        prefix = "paper-" if book == models.BOOK_PAPER else ""
        short_accts = {prefix + a for g in self.books.goals() if g.months_left < SHORT_HORIZON_MONTHS for a in g.account_ids}

        def sell(h, shares: Decimal, why: str):
            limit = (h.price * (1 - CUSHION)).quantize(Decimal("0.01"), ROUND_DOWN)
            value = shares * h.price * h.fx_to_cad
            draft.legs.append(Leg(TradeIntent(h.account_id, h.ticker, "SELL", shares, limit), why, value))
            qty[(h.account_id, h.ticker)] -= shares
            cash[(h.account_id, h.currency)] = cash.get((h.account_id, h.currency), ZERO) + shares * limit
            for b, w in (h.allocation or {"other": 1}).items():
                bv[b] = bv.get(b, ZERO) - value * Decimal(str(w))
            bv["cash"] = bv.get("cash", ZERO) + value

        def tax_note(h, shares) -> str:
            if accounts[h.account_id].account_type in REGISTERED:
                return f" in your {accounts[h.account_id].account_type} (no tax on the sale)"
            gain = (h.price - h.avg_cost) * shares * h.fx_to_cad
            return (f" in {h.account_name}: a taxable account, so this realizes about {usd0(gain)} of capital gains"
                    if gain > 0 else f" in {h.account_name}")

        def order(holders):
            return sorted(holders, key=lambda h: (accounts[h.account_id].account_type not in REGISTERED,
                                                  h.asset_type != "etf", -(h.value_cad or ZERO)))

        # A. rule breaches first
        cap_rule = self.s.rules.get("max_single_stock_pct")
        if cap_rule.enabled:
            for ticker, value in sorted(view.ticker_values().items()):
                first = view.instrument(ticker)
                if first is None or first.asset_type != "stock" or pct(value, total) <= cap_rule.value:
                    continue
                excess = value - total * (cap_rule.value - CAP_BUFFER) / 100
                for h in order([h for h in view.holdings if h.ticker == ticker and h.price]):
                    if excess <= 0:
                        break
                    unit = h.price * h.fx_to_cad
                    shares = min(qty[(h.account_id, ticker)], Decimal(math.ceil(excess / unit)))
                    if shares <= 0:
                        continue
                    sell(h, shares, f"Trim {ticker} back under your {cap_rule.value}% single-stock cap" + tax_note(h, shares))
                    excess -= shares * unit

        # B. drift outside the bands
        targets = self.books.targets()
        tgt_value = {b: total * t.target_pct / 100 for b, t in targets.items()}
        outside = [b for b, t in targets.items()
                   if abs(pct(bv.get(b, ZERO), total) - t.target_pct) > t.band_pct]
        if not outside:
            if draft.legs:
                draft.notes.append("After the trim, every part of your mix is back inside its band.")
            return draft
        for b in [b for b in outside if bv.get(b, ZERO) > tgt_value[b] and b != "cash"]:
            excess = bv[b] - tgt_value[b]
            pure = [h for h in view.holdings if (h.allocation or {}).get(b, 0) >= 0.95 and h.price
                    and qty[(h.account_id, h.ticker)] > 0]
            for h in order(pure):
                if excess < min_trade:
                    break
                unit = h.price * h.fx_to_cad
                shares = min(qty[(h.account_id, h.ticker)], Decimal(int(excess / unit)))
                if shares <= 0 or shares * unit < min_trade:
                    continue
                sell(h, shares, f"Bring {alloc[b].label if b in alloc else b} back toward {targets[b].target_pct:.1f}%"
                     + tax_note(h, shares))
                excess -= shares * unit

        # Buys for what's under target, funded by cash above the cash target (and the floor).
        floor = self.s.rules.get("min_cash_pct").value + CAP_BUFFER
        cash_keep = total * max(targets["cash"].target_pct if "cash" in targets else ZERO, floor) / 100
        spendable = bv.get("cash", ZERO) - cash_keep
        deficits = sorted(((tgt_value[b] - bv.get(b, ZERO), b) for b in targets
                           if b != "cash" and tgt_value[b] > bv.get(b, ZERO)), reverse=True)
        for deficit, b in deficits:
            amount = min(deficit, spendable)
            if amount < min_trade:
                continue
            for (acct, cur), have in sorted(cash.items(), key=lambda kv: -kv[1]):
                if amount < min_trade:
                    break
                if acct in short_accts and b in EQUITY_BUCKETS:
                    continue
                held = next((h for h in view.holdings if h.account_id == acct and h.currency == cur
                             and (h.allocation or {}).get(b, 0) >= 0.95 and h.asset_type == "etf"), None)
                ticker = held.ticker if held else PREFERRED.get((b, cur))
                price = self.books.latest_price(ticker) if ticker else None
                fx, _ = self.books.fx_to_cad(cur)
                if not price or fx is None:
                    continue
                limit = (price.price * (1 + CUSHION)).quantize(Decimal("0.01"))
                budget = min(amount, have * fx)
                shares = Decimal(int(budget / (limit * fx)))
                if shares <= 0 or shares * limit * fx < min_trade:
                    continue
                value = shares * limit * fx
                label = alloc[b].label if b in alloc else b
                draft.legs.append(Leg(TradeIntent(acct, ticker, "BUY", shares, limit),
                                      f"Add to {label} (under its {targets[b].target_pct:.1f}% target) with cash in "
                                      f"{accounts[acct].name}", value))
                cash[(acct, cur)] = have - shares * limit
                amount -= value
                spendable -= value
        usd_idle = sum((v for (a, c), v in cash.items() if c != "CAD" and v > 0), ZERO)
        if usd_idle >= min_trade / 2 and any(l.intent.side == "SELL" for l in draft.legs):
            draft.notes.append("Sale proceeds in USD stay as USD cash: converting to CAD isn't simulated yet, "
                               "so do it at your broker if you want them in Canadian funds.")
        return draft

    def propose_plan(self) -> tuple[list[models.Proposal], AgentReport | None]:
        """Build the plan on the paper book and send it to the Trader for your approval."""
        # Prices for anything the plan might buy.
        wanted = [t for t in PREFERRED.values() if self.books.instrument(t) and not self.books.latest_price(t)]
        if wanted:
            self.s.bookkeeper.refresh_prices(self.s.prices, wanted)
        draft = self.build_plan()
        if not draft.legs:
            return [], None
        open_plans = list(self.s.session.scalars(select(models.Proposal).where(
            models.Proposal.proposed_by == AGENT, models.Proposal.status == "proposed")))
        if any(p.created_at.astimezone(ET).date() == self._today() for p in open_plans):
            return [], None  # one plan a day; yours to approve or dismiss
        for p in open_plans:
            self.s.trader.dismiss(p.id, actor=AGENT, reason="superseded by a newer plan")
        support = [r for r in self.s.desk.recent(limit=50)
                   if r.as_of.astimezone(ET).date() == self._today() and r.can_support_trade
                   and r.agent in {AGENT, "risk_officer"}]
        if not support:
            return [], None  # no sourced reason on file today -> no proposal
        legs = sorted(draft.legs, key=lambda l: l.intent.side != "SELL")  # sells raise the cash buys use
        props = self.s.trader.propose_plan([(l.intent, l.rationale) for l in legs], proposed_by=AGENT,
                                           report_ids=[r.id for r in support])
        now = self.books.now()
        summary = "; ".join(f"{l.intent.side.lower()} {l.intent.quantity} {l.intent.ticker} "
                            f"(~{usd0(l.value_cad)})" for l in legs)
        blocked = [p for p in props if p.status == "blocked"]
        finding = f"Proposed rebalance: {summary}; waiting for your approval"
        if blocked:
            finding += f" ({len(blocked)} leg(s) blocked by the Risk Officer)"
        report = self.s.desk.submit(AgentReport(
            agent=AGENT, as_of=now, subject="portfolio", finding=(finding + ".")[:400],
            stance=Stance.TRIM if any(l.intent.side == "SELL" for l in legs) else Stance.ADD,
            confidence=Decimal("0.7"), urgency=Urgency.MEDIUM,
            evidence=[Evidence(claim=l.rationale, source=f"record:proposals/{p.id}") for l, p in zip(legs, props)]
            + [Evidence(claim=n, source=f"record:proposals/{props[0].group_id}") for n in draft.notes],
            wrong_if="Prices move the mix back inside its bands before you act, or you change your targets.",
            next_check=now + timedelta(days=1),
        ))
        return props, report

    # -- entry point ----------------------------------------------------------------------
    def run(self, *, propose: bool = True) -> list[AgentReport]:
        view = self.books.portfolio()
        errors = [f for f in view.flags if f.severity == "error"]
        now = self.books.now()
        if errors:
            r = self._file(AgentReport(
                agent=AGENT, as_of=now, subject="portfolio",
                finding=f"Advice paused: the books have {len(errors)} data error(s), starting with: {errors[0].message}",
                stance=Stance.NO_VIEW, confidence=Decimal(1), urgency=Urgency.MEDIUM,
                evidence=[Evidence(claim=e.message, source=f"record:flags/{e.code}/{e.subject}") for e in errors],
                wrong_if=None, next_check=now + timedelta(hours=1)))
            return [r] if r else []
        alloc = self.books.allocation(view)
        out = [r for r in [self.drift_report(view, alloc)] if r]
        out += self.goal_reports(view)
        if propose:
            # The Risk Officer's breach reports are a sourced reason for a trim.
            self.s.risk.file_breach_reports(self.s.desk)
            _, plan_report = self.propose_plan()
            if plan_report:
                out.append(plan_report)
        return out
