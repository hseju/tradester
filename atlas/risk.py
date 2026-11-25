"""The Risk Officer: enforces your rules, in code.

It is the only agent with a veto, and the veto lives here, in plain code, so
no AI can argue its way past it. It checks every proposed trade before you
see it (pass or block, with the rule it checked) and scans the portfolio for
rules broken by price moves alone.

Rules have numbers you set. Anyone may TIGHTEN a rule or engage the kill
switch; only you ("user") may loosen a rule, disable one, or release the
kill switch.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from atlas import models
from atlas.bookkeeper import ZERO, Books, PortfolioView, money, pct
from atlas.config import Settings
from atlas.db import utcnow
from atlas.market import ET
from atlas.reports import AgentReport, Evidence, ReportDesk, Stance, Urgency

USER = "user"
KILL_SWITCH_KEY = "kill_switch"


@dataclass(frozen=True)
class RuleSpec:
    key: str
    label: str
    default: Decimal
    unit: str
    stricter: str  # "lower" (a smaller number is stricter) | "higher"
    description: str


RULES: dict[str, RuleSpec] = {r.key: r for r in [
    RuleSpec("max_single_stock_pct", "Max share in any one stock", Decimal("10"), "%", "lower",
             "No single stock may exceed this share of the portfolio. Diversified ETFs are exempt."),
    RuleSpec("max_sector_pct", "Max share in any one sector", Decimal("30"), "%", "lower",
             "Individual stocks in one sector may not exceed this share. Broad funds carry no sector."),
    RuleSpec("min_cash_pct", "Minimum cash floor", Decimal("5"), "%", "higher",
             "Cash across the book may not fall below this share of the portfolio."),
    RuleSpec("headline_cooldown_hours", "No trades after a big headline", Decimal("24"), "h", "higher",
             "No trades on a stock within this many hours of a major headline about it."),
    RuleSpec("daily_loss_limit_cad", "Trader daily loss limit", Decimal("500"), "CAD", "lower",
             "Stop trading for the day once the Trader's trades have lost this much."),
    RuleSpec("monthly_loss_limit_cad", "Trader monthly loss limit", Decimal("2000"), "CAD", "lower",
             "Stop trading for the month once the Trader's trades have lost this much."),
    RuleSpec("max_limit_deviation_pct", "Limit price sanity band", Decimal("5"), "%", "lower",
             "A limit price more than this far from the last price is treated as a typo."),
]}


class RuleChangeRefused(PermissionError):
    pass


@dataclass
class RuleState:
    key: str
    label: str
    value: Decimal
    unit: str
    enabled: bool
    description: str
    updated_by: str


@dataclass
class KillSwitchState:
    engaged: bool
    reason: str = ""
    by: str = ""
    at: str = ""
    forced_by_env: bool = False


class RuleBook:
    def __init__(self, session: Session, settings: Settings | None = None):
        self.session = session
        self.settings = settings or Settings()

    def ensure_defaults(self) -> None:
        for spec in RULES.values():
            if self.session.get(models.RuleSetting, spec.key) is None:
                self.session.add(models.RuleSetting(key=spec.key, value=spec.default, enabled=True, updated_by="system"))
        self.session.flush()

    def get(self, key: str) -> RuleState:
        spec = RULES[key]
        row = self.session.get(models.RuleSetting, key)
        value, enabled, by = (row.value, row.enabled, row.updated_by) if row else (spec.default, True, "default")
        return RuleState(key, spec.label, value, spec.unit, enabled, spec.description, by)

    def all(self) -> list[RuleState]:
        return [self.get(k) for k in RULES]

    def is_loosening(self, key: str, new_value: Decimal | None, new_enabled: bool | None) -> bool:
        cur = self.get(key)
        if new_enabled is False and cur.enabled:
            return True
        if new_value is None:
            return False
        return new_value > cur.value if RULES[key].stricter == "lower" else new_value < cur.value

    def set(self, key: str, value: Decimal | None = None, *, enabled: bool | None = None, actor: str) -> RuleState:
        if key not in RULES:
            raise KeyError(f"unknown rule {key}; one of {sorted(RULES)}")
        if value is not None and value < 0:
            raise ValueError("rule values can't be negative")
        if actor != USER and self.is_loosening(key, value, enabled):
            models.audit(self.session, actor, "rule_change_refused", key=key, value=value, enabled=enabled)
            raise RuleChangeRefused(f"{actor} may tighten rules but only you can loosen '{key}'.")
        row = self.session.get(models.RuleSetting, key)
        before = self.get(key)
        if row is None:
            row = models.RuleSetting(key=key, value=RULES[key].default, enabled=True)
            self.session.add(row)
        if value is not None:
            row.value = value
        if enabled is not None:
            row.enabled = enabled
        row.updated_by = actor
        models.audit(self.session, actor, "rule_change", key=key, before=str(before.value),
                     after=str(row.value), enabled=row.enabled)
        self.session.flush()
        return self.get(key)

    # kill switch --------------------------------------------------------------
    def kill_switch(self) -> KillSwitchState:
        if self.settings.kill_switch_env:
            return KillSwitchState(True, "ATLAS_KILL_SWITCH is set in the environment", "env", "", True)
        row = self.session.get(models.SystemFlag, KILL_SWITCH_KEY)
        if row is None or not row.value.get("engaged"):
            return KillSwitchState(False)
        v = row.value
        return KillSwitchState(True, v.get("reason", ""), v.get("by", ""), v.get("at", ""))

    def engage_kill_switch(self, actor: str, reason: str) -> KillSwitchState:
        self._set_flag({"engaged": True, "reason": reason, "by": actor, "at": utcnow().isoformat()})
        models.audit(self.session, actor, "kill_switch_on", reason=reason)
        # Cancel anything still working at the broker.
        for o in self.session.scalars(select(models.Order).where(models.Order.status == "open")):
            o.status, o.note = "cancelled", "cancelled by kill switch"
        self.session.flush()
        return self.kill_switch()

    def release_kill_switch(self, actor: str, reason: str = "") -> KillSwitchState:
        if actor != USER:
            raise RuleChangeRefused("Only you can release the kill switch.")
        if self.settings.kill_switch_env:
            raise RuleChangeRefused("The kill switch is forced on by ATLAS_KILL_SWITCH; unset it to release.")
        self._set_flag({"engaged": False, "reason": reason, "by": actor, "at": utcnow().isoformat()})
        models.audit(self.session, actor, "kill_switch_off", reason=reason)
        self.session.flush()
        return self.kill_switch()

    def _set_flag(self, value: dict) -> None:
        row = self.session.get(models.SystemFlag, KILL_SWITCH_KEY)
        if row is None:
            self.session.add(models.SystemFlag(key=KILL_SWITCH_KEY, value=value))
        else:
            row.value = value


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class TradeIntent:
    account_id: str
    ticker: str
    side: str  # BUY | SELL
    quantity: Decimal
    limit_price: Decimal | None
    order_type: str = "LIMIT"


@dataclass
class CheckResult:
    rule: str
    label: str
    passed: bool
    detail: str
    value: str | None = None
    limit: str | None = None
    applicable: bool = True


@dataclass
class RiskDecision:
    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    checked_at: datetime = field(default_factory=utcnow)

    @property
    def blocked_by(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checked_at": self.checked_at.isoformat(),
                "checks": [asdict(c) for c in self.checks]}

    def summary(self) -> str:
        if self.passed:
            return f"PASS - {sum(c.applicable for c in self.checks)} rule checks clear"
        return "BLOCK - " + "; ".join(f"{c.label}: {c.detail}" for c in self.blocked_by)


@dataclass
class Breach:
    rule: str
    label: str
    subject: str
    value_pct: Decimal
    limit_pct: Decimal
    detail: str
    evidence: list[Evidence]


@dataclass
class ProForma:
    """The book as it would stand after the legs checked so far (for multi-leg plans)."""

    total: Decimal
    cash_cad: Decimal
    ticker_values: dict[str, Decimal]
    sector_values: dict[str, Decimal]
    account_cash: dict[tuple[str, str], Decimal]
    account_qty: dict[tuple[str, str], Decimal]

    @classmethod
    def from_view(cls, view: PortfolioView) -> "ProForma":
        cash: dict[tuple[str, str], Decimal] = {}
        for c in view.cash:
            cash[(c.account_id, c.currency)] = cash.get((c.account_id, c.currency), ZERO) + c.amount
        qty: dict[tuple[str, str], Decimal] = {}
        for h in view.holdings:
            qty[(h.account_id, h.ticker)] = qty.get((h.account_id, h.ticker), ZERO) + h.quantity
        return cls(view.total_cad, view.cash_cad, view.ticker_values(), view.sector_values(), cash, qty)

    def apply(self, intent: "TradeIntent", currency: str, sector: str | None, price: Decimal, fx: Decimal) -> None:
        native = intent.quantity * price
        cad = native * fx
        sign = 1 if intent.side.upper() == "BUY" else -1
        key_c, key_q = (intent.account_id, currency), (intent.account_id, intent.ticker)
        self.account_cash[key_c] = self.account_cash.get(key_c, ZERO) - sign * native
        self.account_qty[key_q] = self.account_qty.get(key_q, ZERO) + sign * intent.quantity
        self.cash_cad -= sign * cad
        self.ticker_values[intent.ticker] = self.ticker_values.get(intent.ticker, ZERO) + sign * cad
        if sector:
            self.sector_values[sector] = self.sector_values.get(sector, ZERO) + sign * cad


class RiskOfficer:
    actor = "risk_officer"

    def __init__(self, books: Books, rules: RuleBook):
        self.books = books
        self.rules = rules

    # --- pre-trade -----------------------------------------------------------
    def check_plan(self, intents: list[TradeIntent]) -> list[RiskDecision]:
        """Check legs in order; each passing leg is applied before the next is checked,
        so a buy can rely on the cash an earlier sell in the same plan raises."""
        state: ProForma | None = None
        out = []
        for intent in intents:
            acct = self.books.account(intent.account_id)
            if state is None and acct is not None:
                state = ProForma.from_view(self.books.portfolio(acct.book))
            out.append(self.check_trade(intent, state=state, apply=True))
        return out

    def check_trade(self, intent: TradeIntent, *, state: ProForma | None = None, apply: bool = False) -> RiskDecision:
        checks: list[CheckResult] = []
        add = checks.append
        ks = self.rules.kill_switch()
        add(CheckResult("kill_switch", "Kill switch", not ks.engaged,
                        f"ENGAGED by {ks.by}: {ks.reason}" if ks.engaged else "off"))

        side = intent.side.upper()
        acct = self.books.account(intent.account_id)
        inst = self.books.instrument(intent.ticker)
        if side not in {"BUY", "SELL"} or intent.quantity is None or intent.quantity <= 0:
            add(CheckResult("order_shape", "Valid order", False, "side must be BUY/SELL and quantity positive"))
            return RiskDecision(False, checks)
        if acct is None or inst is None:
            add(CheckResult("known_instrument", "Known account and instrument", False,
                            f"unknown {'account ' + intent.account_id if acct is None else 'instrument ' + intent.ticker}"))
            return RiskDecision(False, checks)

        # Limit orders only (Trader safety rule, not configurable)
        is_limit = intent.order_type.upper() == "LIMIT" and intent.limit_price is not None and intent.limit_price > 0
        add(CheckResult("limit_orders_only", "Limit orders only", is_limit,
                        "limit order" if is_limit else "market orders are not allowed; set a maximum price"))

        if state is None:
            state = ProForma.from_view(self.books.portfolio(acct.book))
        price_row = self.books.latest_price(intent.ticker)
        fx, _ = self.books.fx_to_cad(inst.currency)
        fresh = price_row is not None and not self.books.is_stale(price_row.as_of) and fx is not None
        add(CheckResult("fresh_data", "Fresh price and FX", fresh,
                        "ok" if fresh else ("no price" if price_row is None else
                                            "no FX rate" if fx is None else f"price is stale (as of {price_row.as_of:%Y-%m-%d %H:%M} UTC)")))
        if not fresh:
            return RiskDecision(False, checks)
        last = price_row.price

        # Sanity band on the limit price
        dev_rule = self.rules.get("max_limit_deviation_pct")
        if is_limit and dev_rule.enabled:
            dev = abs(intent.limit_price - last) / last * 100
            add(CheckResult(dev_rule.key, dev_rule.label, dev <= dev_rule.value,
                            f"limit {intent.limit_price} vs last {last} ({dev:.1f}% away)",
                            f"{dev:.2f}%", f"{dev_rule.value}%"))

        # Funds / shares in the account
        qty = intent.quantity
        ref_px = intent.limit_price if (side == "BUY" and is_limit) else last
        notional_native = qty * ref_px
        if side == "BUY":
            have = state.account_cash.get((acct.id, inst.currency), ZERO)
            add(CheckResult("funds", "Enough cash in the account", notional_native <= have,
                            f"needs {money(notional_native)} {inst.currency}, has {money(have)} {inst.currency}"))
        else:
            held = state.account_qty.get((acct.id, intent.ticker), ZERO)
            add(CheckResult("funds", "Enough shares in the account", qty <= held, f"selling {qty}, holds {held}"))

        # Pro-forma portfolio after the trade (total value unchanged; cash <-> position)
        notional = notional_native * fx
        total = state.total
        tv = state.ticker_values
        sectors = state.sector_values
        cash_after = state.cash_cad - notional if side == "BUY" else state.cash_cad + notional
        delta = notional if side == "BUY" else -notional
        tv_after = tv.get(intent.ticker, ZERO) + delta

        stock_rule = self.rules.get("max_single_stock_pct")
        if stock_rule.enabled:
            if inst.asset_type != "stock":
                add(CheckResult(stock_rule.key, stock_rule.label, True, "n/a: diversified fund", applicable=False))
            else:
                before_w, after_w = pct(tv.get(intent.ticker, ZERO), total), pct(tv_after, total)
                ok = side == "SELL" or after_w <= stock_rule.value
                add(CheckResult(stock_rule.key, stock_rule.label, ok,
                                f"{intent.ticker} {before_w:.1f}% -> {after_w:.1f}%"
                                + ("" if ok else f", over the {stock_rule.value}% cap"),
                                f"{after_w:.2f}%", f"{stock_rule.value}%"))

        sector_rule = self.rules.get("max_sector_pct")
        if sector_rule.enabled:
            if not inst.sector or inst.asset_type != "stock":
                add(CheckResult(sector_rule.key, sector_rule.label, True, "n/a: no single sector", applicable=False))
            else:
                after = pct(sectors.get(inst.sector, ZERO) + delta, total)
                ok = side == "SELL" or after <= sector_rule.value
                add(CheckResult(sector_rule.key, sector_rule.label, ok, f"{inst.sector} -> {after:.1f}%",
                                f"{after:.2f}%", f"{sector_rule.value}%"))

        cash_rule = self.rules.get("min_cash_pct")
        if cash_rule.enabled:
            after = pct(cash_after, total)
            ok = side == "SELL" or after >= cash_rule.value
            add(CheckResult(cash_rule.key, cash_rule.label, ok, f"cash {pct(state.cash_cad, total):.1f}% -> {after:.1f}%",
                            f"{after:.2f}%", f"{cash_rule.value}%"))

        news_rule = self.rules.get("headline_cooldown_hours")
        if news_rule.enabled:
            since = self.books.now() - timedelta(hours=float(news_rule.value))
            major = [h for h in self.books.headlines(intent.ticker, since=since, limit=50) if h.is_major]
            add(CheckResult(news_rule.key, news_rule.label, not major,
                            f"major headline {major[0].published_at:%Y-%m-%d %H:%M} UTC: {major[0].headline[:90]}"
                            if major else f"no major headlines in {news_rule.value}h"))

        for key, since in (("daily_loss_limit_cad", self._day_start()), ("monthly_loss_limit_cad", self._month_start())):
            r = self.rules.get(key)
            if not r.enabled:
                continue
            loss = -min(ZERO, self.trader_pnl_cad(acct.book, since))
            add(CheckResult(key, r.label, loss < r.value, f"Trader loss {money(loss)} CAD of {r.value} limit",
                            str(money(loss)), str(r.value)))

        decision = RiskDecision(all(c.passed for c in checks), checks)
        if apply and decision.passed:
            # Conservative: a buy costs at most its limit; a sell raises at least its limit.
            plan_px = intent.limit_price if is_limit else last
            state.apply(intent, inst.currency, inst.sector if inst.asset_type == "stock" else None, plan_px, fx)
        return decision

    def _day_start(self) -> datetime:
        local = self.books.now().astimezone(ET)
        return datetime.combine(local.date(), time(0), tzinfo=ET)

    def _month_start(self) -> datetime:
        local = self.books.now().astimezone(ET)
        return datetime.combine(local.date().replace(day=1), time(0), tzinfo=ET)

    def trader_pnl_cad(self, book: str, since: datetime) -> Decimal:
        """P&L caused by the Trader's own fills since `since`: realized on sells,
        marked to the latest price on buys."""
        q = (
            select(models.Order)
            .join(models.Account, models.Order.account_id == models.Account.id)
            .where(models.Account.book == book, models.Order.status == "filled", models.Order.filled_at >= since)
        )
        total = ZERO
        for o in self.books.session.scalars(q):
            inst = self.books.instrument(o.ticker)
            fx, _ = self.books.fx_to_cad(inst.currency if inst else "CAD")
            fx = fx or Decimal(1)
            if o.side == "SELL":
                total += (o.realized_pnl or ZERO) * fx
            else:
                p = self.books.latest_price(o.ticker)
                if p and o.fill_price is not None:
                    total += (p.price - o.fill_price) * (o.fill_quantity or o.quantity) * fx
        return total

    # --- continuous scan --------------------------------------------------------
    def scan(self, book: str = models.BOOK_REAL, view: PortfolioView | None = None) -> list[Breach]:
        """Rules broken by price moves alone (no trade involved)."""
        view = view or self.books.portfolio(book)
        out: list[Breach] = []
        total = view.total_cad
        if not total:
            return out
        stock_rule = self.rules.get("max_single_stock_pct")
        if stock_rule.enabled:
            for ticker, value in sorted(view.ticker_values().items()):
                h = view.instrument(ticker)
                if h is None or h.asset_type != "stock":
                    continue
                w = pct(value, total)
                if w > stock_rule.value:
                    excess = value - total * stock_rule.value / 100
                    out.append(Breach(
                        stock_rule.key, stock_rule.label, ticker, w, stock_rule.value,
                        f"{ticker} is {w:.1f}% of the portfolio; the cap is {stock_rule.value}%. "
                        f"About ${money(excess):,} CAD over.",
                        [Evidence(claim=f"{ticker} position value {money(value)} CAD across accounts",
                                  source=h.price_record),
                         Evidence(claim=f"Portfolio total {money(total)} CAD", source="record:portfolio/" + book),
                         Evidence(claim=f"Single-stock cap set to {stock_rule.value}%",
                                  source=f"record:rule_settings/{stock_rule.key}")],
                    ))
        sector_rule = self.rules.get("max_sector_pct")
        if sector_rule.enabled:
            for sector, value in sorted(view.sector_values().items()):
                w = pct(value, total)
                if w > sector_rule.value:
                    out.append(Breach(
                        sector_rule.key, sector_rule.label, sector, w, sector_rule.value,
                        f"{sector} stocks are {w:.1f}% of the portfolio; the cap is {sector_rule.value}%.",
                        [Evidence(claim=f"{sector} stocks total {money(value)} CAD", source="record:portfolio/" + book),
                         Evidence(claim=f"Sector cap set to {sector_rule.value}%",
                                  source=f"record:rule_settings/{sector_rule.key}")],
                    ))
        cash_rule = self.rules.get("min_cash_pct")
        if cash_rule.enabled:
            w = pct(view.cash_cad, total)
            if w < cash_rule.value:
                out.append(Breach(
                    cash_rule.key, cash_rule.label, "portfolio", w, cash_rule.value,
                    f"Cash is {w:.1f}% of the portfolio, under the {cash_rule.value}% floor.",
                    [Evidence(claim=f"Cash {money(view.cash_cad)} CAD of {money(total)} CAD",
                              source="record:portfolio/" + book)],
                ))
        return out

    def file_breach_reports(self, desk: ReportDesk, book: str = models.BOOK_REAL) -> list[AgentReport]:
        """One shared-format report per breach. Skips a breach already reported today at the same level."""
        now = self.books.now()
        filed = []
        for b in self.scan(book):
            prior = desk.recent(agent=self.actor, subject=b.subject, limit=1)
            if prior and prior[0].as_of.astimezone(ET).date() == now.astimezone(ET).date():
                m = re.search(r" (?:is|are) (\d+(?:\.\d+)?)% of the portfolio", prior[0].finding)
                if m is None or abs(Decimal(m.group(1)) - b.value_pct) < 1:
                    continue  # already reported today at about this level
            stance = Stance.TRIM if b.rule in {"max_single_stock_pct", "max_sector_pct"} else Stance.HOLD
            report = AgentReport(
                agent=self.actor, as_of=now, subject=b.subject, finding=b.detail, stance=stance,
                confidence=Decimal("0.95"),
                urgency=Urgency.HIGH if b.value_pct - b.limit_pct > 2 or b.rule == "min_cash_pct" else Urgency.MEDIUM,
                evidence=b.evidence,
                wrong_if=(f"Price moves bring {b.subject} back within {b.limit_pct}% on their own, "
                          "or you change the rule."),
                next_check=now + timedelta(minutes=15),
            )
            filed.append(desk.submit(report))
        return filed
