"""The Bookkeeper: holds the single source of truth.

Holdings, cash, prices, FX, goals and targets. It is ordinary code, not AI,
because the numbers have to be exact. It reports facts and never opinions;
it flags missing prices, stale data, and numbers that don't add up so no
other agent works from bad data.

  Books       - read-only view. This is what every agent (and Atlas) gets.
  Bookkeeper  - Books + the write methods. Only the Bookkeeper and the
                Trader (through fills) change the books.

All values are reported in CAD (the base currency). Cost basis for foreign
holdings is converted at today's FX rate - an approximation; tax-exact
adjusted cost base (FX at purchase) is a Tax & Accounts job for later.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from atlas import models
from atlas.brokers.base import BrokerAdapter
from atlas.brokers.paper import InsufficientFunds
from atlas.config import BASE_CURRENCY, Settings
from atlas.db import utcnow
from atlas.market import currency_for, is_market_open, make_ticker, parse_ticker
from atlas.prices.base import NewsItem
from atlas.prices.service import PriceService
from atlas.reports import AgentReport, Evidence, ReportDesk, Stance, Urgency

BUCKETS = ["ca_equity", "us_equity", "intl_equity", "fixed_income", "cash", "other"]
BUCKET_LABELS = {
    "ca_equity": "Canadian stocks",
    "us_equity": "US stocks",
    "intl_equity": "International stocks",
    "fixed_income": "Bonds",
    "cash": "Cash",
    "other": "Other",
}
# Long-run return assumptions per bucket (% a year, nominal). Yours to set: `atlas assume BUCKET PCT`.
DEFAULT_ASSUMPTIONS = {
    "expected_return_pct": {"ca_equity": "6.0", "us_equity": "6.0", "intl_equity": "6.0",
                            "fixed_income": "3.5", "cash": "2.5", "other": "4.0"},
    "min_trade_cad": "200",
}
ASSUMPTIONS_KEY = "advisor_assumptions"
EQUITY_BUCKETS = ("ca_equity", "us_equity", "intl_equity")
ZERO = Decimal(0)
CENT = Decimal("0.01")
MAJOR_HEADLINE_WORDS = (
    "earnings", "guidance", "downgrade", "upgrade", "lawsuit", "sec ", "investigation", "acquire",
    "acquisition", "merger", "takeover", "recall", "bankrupt", "halt", "ceo ", "resigns", "fraud",
    "dividend cut", "restate", "profit warning", "antitrust", "probe",
)


def money(d: Decimal | None) -> Decimal | None:
    return None if d is None else d.quantize(CENT)


def pct(part: Decimal, whole: Decimal) -> Decimal:
    return ZERO if not whole else (part / whole * 100)


# --------------------------------------------------------------------------
# Views (plain data handed to agents)
# --------------------------------------------------------------------------
@dataclass
class DataFlag:
    severity: str  # info | warn | error
    code: str  # missing_price | stale_price | sample_data | missing_fx | unclassified | integrity | reconcile
    subject: str
    message: str


@dataclass
class HoldingView:
    account_id: str
    account_name: str
    account_type: str
    ticker: str
    name: str
    asset_type: str
    sector: str | None
    allocation: dict
    currency: str
    quantity: Decimal
    avg_cost: Decimal
    price: Decimal | None
    prev_close: Decimal | None
    price_as_of: datetime | None
    price_source: str | None
    price_record: str | None
    stale: bool
    fx_to_cad: Decimal | None
    value_cad: Decimal | None
    cost_cad: Decimal | None
    weight_pct: Decimal = ZERO

    @property
    def unrealized_cad(self) -> Decimal | None:
        if self.value_cad is None or self.cost_cad is None:
            return None
        return self.value_cad - self.cost_cad

    @property
    def unrealized_pct(self) -> Decimal | None:
        if self.unrealized_cad is None or not self.cost_cad:
            return None
        return self.unrealized_cad / self.cost_cad * 100

    @property
    def day_change_cad(self) -> Decimal | None:
        if self.price is None or self.prev_close is None or self.fx_to_cad is None:
            return None
        return (self.price - self.prev_close) * self.quantity * self.fx_to_cad


@dataclass
class CashView:
    account_id: str
    account_name: str
    currency: str
    amount: Decimal
    fx_to_cad: Decimal | None
    value_cad: Decimal | None


@dataclass
class AccountSummary:
    id: str
    name: str
    account_type: str
    broker: str
    invested_cad: Decimal
    cash_cad: Decimal
    total_cad: Decimal


@dataclass
class BucketView:
    bucket: str
    label: str
    value_cad: Decimal
    pct: Decimal
    target_pct: Decimal | None
    band_pct: Decimal | None

    @property
    def drift_pct(self) -> Decimal | None:
        return None if self.target_pct is None else self.pct - self.target_pct

    @property
    def outside_band(self) -> bool:
        return self.drift_pct is not None and self.band_pct is not None and abs(self.drift_pct) > self.band_pct


@dataclass
class GoalView:
    id: str
    name: str
    target_amount: Decimal
    target_date: date
    priority: int
    account_ids: list[str]
    current_cad: Decimal
    progress_pct: Decimal
    months_left: int
    # Plain arithmetic, no growth assumed. The Advisor does the pacing with return assumptions.
    needed_per_month_no_growth: Decimal | None
    monthly_contribution: Decimal = ZERO


@dataclass
class PortfolioView:
    book: str
    as_of: datetime
    holdings: list[HoldingView]
    cash: list[CashView]
    accounts: list[AccountSummary]
    flags: list[DataFlag]
    invested_cad: Decimal
    cash_cad: Decimal
    total_cad: Decimal
    day_change_cad: Decimal | None
    uses_sample_data: bool

    # Aggregates across accounts -----------------------------------------
    def ticker_values(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for h in self.holdings:
            if h.value_cad is not None:
                out[h.ticker] += h.value_cad
        return dict(out)

    def ticker_weight(self, ticker: str) -> Decimal:
        return pct(self.ticker_values().get(ticker, ZERO), self.total_cad)

    def instrument(self, ticker: str) -> HoldingView | None:
        return next((h for h in self.holdings if h.ticker == ticker), None)

    def sector_values(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for h in self.holdings:
            if h.value_cad is not None and h.sector:
                out[h.sector] += h.value_cad
        return dict(out)

    def flags_for(self, ticker: str) -> list[DataFlag]:
        return [f for f in self.flags if f.subject == ticker]

    def cash_in(self, account_id: str, currency: str) -> Decimal:
        return sum((c.amount for c in self.cash if c.account_id == account_id and c.currency == currency), ZERO)

    def position_qty(self, account_id: str, ticker: str) -> Decimal:
        return sum((h.quantity for h in self.holdings if h.account_id == account_id and h.ticker == ticker), ZERO)


# --------------------------------------------------------------------------
# Books: read-only
# --------------------------------------------------------------------------
class Books:
    def __init__(self, session: Session, settings: Settings | None = None, now=utcnow):
        self.session = session
        self.settings = settings or Settings()
        self._now = now

    def now(self) -> datetime:
        return self._now()

    # basic reads -----------------------------------------------------------
    def accounts(self, book: str = models.BOOK_REAL) -> list[models.Account]:
        q = select(models.Account).where(models.Account.book == book).order_by(models.Account.id)
        return list(self.session.scalars(q))

    def account(self, account_id: str) -> models.Account | None:
        return self.session.get(models.Account, account_id)

    def instrument(self, ticker: str) -> models.Instrument | None:
        return self.session.get(models.Instrument, ticker)

    def latest_price(self, ticker: str) -> models.Price | None:
        q = (
            select(models.Price)
            .where(models.Price.ticker == ticker)
            .order_by(models.Price.as_of.desc(), models.Price.id.desc())
            .limit(1)
        )
        return self.session.scalars(q).first()

    def latest_fx(self, pair: str) -> models.FxRate | None:
        q = select(models.FxRate).where(models.FxRate.pair == pair).order_by(
            models.FxRate.as_of.desc(), models.FxRate.id.desc()).limit(1)
        return self.session.scalars(q).first()

    def fx_to_cad(self, currency: str) -> tuple[Decimal | None, models.FxRate | None]:
        if currency == BASE_CURRENCY:
            return Decimal(1), None
        row = self.latest_fx(f"{currency}{BASE_CURRENCY}")
        return (row.rate, row) if row else (None, None)

    def is_stale(self, as_of: datetime, now: datetime | None = None) -> bool:
        now = now or self.now()
        age = now - as_of
        if is_market_open(now):
            return age > timedelta(minutes=self.settings.intraday_stale_minutes)
        return age > timedelta(hours=self.settings.closed_stale_hours)

    def tracked_tickers(self) -> list[str]:
        q = select(models.Position.ticker).distinct()
        return sorted(self.session.scalars(q))

    def targets(self) -> dict[str, models.TargetAllocation]:
        return {t.bucket: t for t in self.session.scalars(select(models.TargetAllocation))}

    def headlines(self, ticker: str | None = None, since: datetime | None = None, limit: int = 20) -> list[models.Headline]:
        q = select(models.Headline).order_by(models.Headline.published_at.desc())
        if ticker:
            q = q.where(models.Headline.ticker == ticker)
        if since:
            q = q.where(models.Headline.published_at >= since)
        return list(self.session.scalars(q.limit(limit)))

    def orders(self, book: str = models.BOOK_PAPER, limit: int = 20) -> list[models.Order]:
        q = (
            select(models.Order)
            .join(models.Account, models.Order.account_id == models.Account.id)
            .where(models.Account.book == book)
            .order_by(models.Order.created_at.desc())
            .limit(limit)
        )
        return list(self.session.scalars(q))

    def net_worth_history(self, book: str = models.BOOK_REAL, days: int = 30) -> list[models.NetWorthSnapshot]:
        since = self.now() - timedelta(days=days)
        q = (
            select(models.NetWorthSnapshot)
            .where(models.NetWorthSnapshot.book == book, models.NetWorthSnapshot.taken_at >= since)
            .order_by(models.NetWorthSnapshot.taken_at)
        )
        return list(self.session.scalars(q))

    def assumptions(self) -> dict:
        row = self.session.get(models.SystemFlag, ASSUMPTIONS_KEY)
        stored = row.value if row else {}
        er = {**DEFAULT_ASSUMPTIONS["expected_return_pct"], **stored.get("expected_return_pct", {})}
        return {"expected_return_pct": {k: Decimal(v) for k, v in er.items()},
                "min_trade_cad": Decimal(stored.get("min_trade_cad", DEFAULT_ASSUMPTIONS["min_trade_cad"]))}

    def watchlist(self) -> list[models.WatchItem]:
        return list(self.session.scalars(select(models.WatchItem).order_by(models.WatchItem.ticker)))

    def universe(self) -> list[str]:
        """Everything Market Watch cares about: what you own (either book) and what you watch."""
        return sorted(set(self.tracked_tickers()) | {w.ticker for w in self.watchlist()})

    def upcoming_events(self, days: int = 7, kinds: tuple[str, ...] | None = None) -> list[models.CalendarEvent]:
        now = self.now()
        q = (select(models.CalendarEvent)
             .where(models.CalendarEvent.at >= now - timedelta(hours=12), models.CalendarEvent.at <= now + timedelta(days=days))
             .order_by(models.CalendarEvent.at))
        if kinds:
            q = q.where(models.CalendarEvent.kind.in_(kinds))
        return list(self.session.scalars(q))

    def fx_change(self, pair: str = "USDCAD") -> tuple[models.FxRate | None, models.FxRate | None]:
        """(latest rate, latest rate from an earlier day) - for 'the loonie moved' checks."""
        latest = self.latest_fx(pair)
        if latest is None:
            return None, None
        day_start = latest.as_of.replace(hour=0, minute=0, second=0, microsecond=0)
        prev = self.session.scalars(
            select(models.FxRate).where(models.FxRate.pair == pair, models.FxRate.as_of < day_start)
            .order_by(models.FxRate.as_of.desc(), models.FxRate.id.desc()).limit(1)).first()
        return latest, prev

    # the main view ---------------------------------------------------------
    def portfolio(self, book: str = models.BOOK_REAL) -> PortfolioView:
        now = self.now()
        flags: list[DataFlag] = []
        accounts = {a.id: a for a in self.accounts(book)}
        holdings: list[HoldingView] = []
        cash_lines: list[CashView] = []
        fx_cache: dict[str, Decimal | None] = {}
        sample_seen = False

        def fx(cur: str) -> Decimal | None:
            if cur not in fx_cache:
                rate, row = self.fx_to_cad(cur)
                fx_cache[cur] = rate
                if rate is None:
                    flags.append(DataFlag("error", "missing_fx", cur,
                                          f"No {cur}->CAD rate; {cur} amounts are left out of totals."))
                elif row is not None and self.is_stale(row.as_of, now):
                    flags.append(DataFlag("warn", "stale_fx", cur, f"{cur}CAD rate is from {row.as_of:%Y-%m-%d %H:%M} UTC."))
            return fx_cache[cur]

        if not accounts:
            return PortfolioView(book, now, [], [], [], [DataFlag("warn", "empty", "portfolio", "No accounts in the books.")],
                                 ZERO, ZERO, ZERO, None, False)

        pos_q = (
            select(models.Position, models.Instrument)
            .join(models.Instrument, models.Position.ticker == models.Instrument.ticker)
            .where(models.Position.account_id.in_(accounts))
            .order_by(models.Position.account_id, models.Position.ticker)
        )
        price_cache: dict[str, models.Price | None] = {}
        for pos, inst in self.session.execute(pos_q):
            acct = accounts[pos.account_id]
            if pos.quantity < 0:
                flags.append(DataFlag("error", "integrity", inst.ticker,
                                      f"Negative quantity {pos.quantity} in {acct.name} (short positions aren't supported)."))
            if pos.quantity == 0:
                continue
            if inst.ticker not in price_cache:
                price_cache[inst.ticker] = self.latest_price(inst.ticker)
            p = price_cache[inst.ticker]
            stale = False
            if p is None:
                flags.append(DataFlag("error", "missing_price", inst.ticker, f"No price for {inst.ticker}; left out of totals."))
            else:
                if p.source == "sample":
                    sample_seen = True
                stale = self.is_stale(p.as_of, now)
                if stale and not any(f.code == "stale_price" and f.subject == inst.ticker for f in flags):
                    flags.append(DataFlag("warn", "stale_price", inst.ticker,
                                          f"{inst.ticker} price is from {p.as_of:%Y-%m-%d %H:%M} UTC ({p.source})."))
            if not inst.classified and not any(f.code == "unclassified" and f.subject == inst.ticker for f in flags):
                flags.append(DataFlag("info", "unclassified", inst.ticker,
                                      f"{inst.ticker} has no catalogue entry; its mix and sector are guesses."))
            rate = fx(inst.currency)
            value = cost = None
            if p is not None and rate is not None:
                value = pos.quantity * p.price * rate
                cost = pos.quantity * pos.avg_cost * rate
            holdings.append(HoldingView(
                account_id=acct.id, account_name=acct.name, account_type=acct.account_type,
                ticker=inst.ticker, name=inst.name, asset_type=inst.asset_type, sector=inst.sector,
                allocation=inst.allocation or {}, currency=inst.currency, quantity=pos.quantity,
                avg_cost=pos.avg_cost, price=p.price if p else None, prev_close=p.prev_close if p else None,
                price_as_of=p.as_of if p else None, price_source=p.source if p else None,
                price_record=f"record:prices/{p.id}" if p else None, stale=stale, fx_to_cad=rate,
                value_cad=value, cost_cad=cost,
            ))

        for c in self.session.scalars(
            select(models.CashBalance).where(models.CashBalance.account_id.in_(accounts))
            .order_by(models.CashBalance.account_id, models.CashBalance.currency)
        ):
            if c.amount < 0:
                flags.append(DataFlag("error", "integrity", c.account_id,
                                      f"Negative {c.currency} cash ({c.amount}) in {accounts[c.account_id].name}."))
            rate = fx(c.currency)
            cash_lines.append(CashView(c.account_id, accounts[c.account_id].name, c.currency, c.amount, rate,
                                       None if rate is None else c.amount * rate))

        invested = sum((h.value_cad for h in holdings if h.value_cad is not None), ZERO)
        cash_total = sum((c.value_cad for c in cash_lines if c.value_cad is not None), ZERO)
        total = invested + cash_total
        for h in holdings:
            h.weight_pct = pct(h.value_cad or ZERO, total)

        summaries = []
        for a in accounts.values():
            inv = sum((h.value_cad or ZERO for h in holdings if h.account_id == a.id), ZERO)
            csh = sum((c.value_cad or ZERO for c in cash_lines if c.account_id == a.id), ZERO)
            summaries.append(AccountSummary(a.id, a.name, a.account_type, a.broker, inv, csh, inv + csh))

        # Numbers must add up: accounts sum to the total, weights to 100%.
        acct_sum = sum((s.total_cad for s in summaries), ZERO)
        if abs(acct_sum - total) > CENT:
            flags.append(DataFlag("error", "integrity", "portfolio", f"Accounts sum to {acct_sum}, total is {total}."))
        if total and holdings:
            wsum = sum((h.weight_pct for h in holdings), ZERO) + pct(cash_total, total)
            if abs(wsum - 100) > Decimal("0.01"):
                flags.append(DataFlag("error", "integrity", "portfolio", f"Weights sum to {wsum:.4f}%, not 100%."))
        if sample_seen:
            flags.append(DataFlag("info", "sample_data", "portfolio",
                                  "Prices are SAMPLE placeholders, not market data. Add EODHD/Finnhub keys for real prices."))

        day = [h.day_change_cad for h in holdings if h.day_change_cad is not None]
        return PortfolioView(
            book=book, as_of=now, holdings=holdings, cash=cash_lines, accounts=summaries, flags=flags,
            invested_cad=invested, cash_cad=cash_total, total_cad=total,
            day_change_cad=sum(day, ZERO) if day else None, uses_sample_data=sample_seen,
        )

    def allocation(self, view: PortfolioView | None = None, account_ids: list[str] | None = None) -> list[BucketView]:
        """Mix vs target, for the whole book or (account_ids) a subset such as a goal's accounts."""
        view = view or self.portfolio()
        keep = set(account_ids) if account_ids is not None else None
        values = {b: ZERO for b in BUCKETS}
        for h in view.holdings:
            if h.value_cad is None or (keep is not None and h.account_id not in keep):
                continue
            mix = h.allocation or {"other": 1}
            total_w = sum((Decimal(str(w)) for w in mix.values()), ZERO) or Decimal(1)
            for bucket, w in mix.items():
                values[bucket if bucket in values else "other"] += h.value_cad * Decimal(str(w)) / total_w
        values["cash"] += sum((c.value_cad or ZERO for c in view.cash if keep is None or c.account_id in keep), ZERO)
        total = sum(values.values(), ZERO)
        targets = self.targets()
        out = []
        for b in BUCKETS:
            t = targets.get(b)
            if values[b] == 0 and t is None:
                continue
            out.append(BucketView(b, BUCKET_LABELS[b], values[b], pct(values[b], total),
                                  t.target_pct if t else None, t.band_pct if t else None))
        return out

    def goals(self, view: PortfolioView | None = None) -> list[GoalView]:
        view = view or self.portfolio()
        by_acct = {a.id: a.total_cad for a in view.accounts}
        today = self.now().date()
        out = []
        for g in self.session.scalars(select(models.Goal).order_by(models.Goal.priority, models.Goal.id)):
            cur = sum((by_acct.get(a, ZERO) for a in g.account_ids), ZERO)
            months = max(0, (g.target_date.year - today.year) * 12 + (g.target_date.month - today.month))
            remaining = max(ZERO, g.target_amount - cur)
            out.append(GoalView(g.id, g.name, g.target_amount, g.target_date, g.priority, list(g.account_ids), cur,
                                pct(cur, g.target_amount), months,
                                (remaining / months) if months else (remaining or None),
                                g.monthly_contribution or ZERO))
        return out


# --------------------------------------------------------------------------
# Bookkeeper: Books + writes
# --------------------------------------------------------------------------
@dataclass
class ImportResult:
    accounts: int = 0
    positions: int = 0
    cash_lines: int = 0
    new_instruments: list[str] = field(default_factory=list)
    flags: list[DataFlag] = field(default_factory=list)


@dataclass
class RefreshResult:
    updated: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    fx: dict[str, str] = field(default_factory=dict)


class Bookkeeper(Books):
    actor = "bookkeeper"

    # instruments -----------------------------------------------------------
    def load_catalog(self, path: Path) -> int:
        """Load instrument metadata (name, type, sector, look-through mix, fee) from CSV."""
        n = 0
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                sym, exch = parse_ticker(row["ticker"])
                mix = {b: float(row[b]) for b in BUCKETS if row.get(b) not in (None, "", "0", "0.0")}
                self.upsert_instrument(
                    f"{sym}.{exch}", name=row.get("name", ""), asset_type=row.get("asset_type") or "stock",
                    sector=row.get("sector") or None, allocation=mix,
                    mer_pct=Decimal(row["mer_pct"]) if row.get("mer_pct") else None, classified=True,
                )
                n += 1
        return n

    def upsert_instrument(self, ticker: str, *, name: str = "", asset_type: str = "stock", sector: str | None = None,
                          allocation: dict | None = None, mer_pct: Decimal | None = None,
                          classified: bool = True) -> models.Instrument:
        sym, exch = parse_ticker(ticker)
        inst = self.session.get(models.Instrument, ticker)
        if inst is None:
            inst = models.Instrument(ticker=ticker, symbol=sym, exchange=exch, currency=currency_for(exch))
            self.session.add(inst)
        inst.name = name or inst.name or sym
        inst.asset_type = asset_type
        inst.sector = sector
        inst.allocation = allocation or {}
        inst.mer_pct = mer_pct
        inst.classified = classified
        return inst

    def _ensure_instrument(self, ticker: str, name: str = "", asset_type: str | None = None) -> bool:
        """Create a best-guess instrument if unknown. Returns True if it was new."""
        if self.session.get(models.Instrument, ticker):
            return False
        _, exch = parse_ticker(ticker)
        guess = "us_equity" if exch == "US" else "ca_equity"
        is_fund = (asset_type or "").lower() in {"etf", "fund", "mutual fund", "exchange traded fund"}
        self.upsert_instrument(ticker, name=name, asset_type="etf" if is_fund else "stock",
                               sector=None, allocation={guess: 1}, classified=False)
        return True

    # imports ---------------------------------------------------------------
    def import_from_broker(self, broker: BrokerAdapter, *, book: str = models.BOOK_REAL,
                           replace: bool = True, actor: str = "user") -> ImportResult:
        """Pull accounts, holdings and cash from a broker into the books.

        With replace=True each imported account's positions and cash are
        replaced wholesale by what the broker reports (a full snapshot).
        """
        res = ImportResult()
        for ba in broker.list_accounts():
            acct = self.session.get(models.Account, ba.id)
            if acct is None:
                acct = models.Account(id=ba.id)
                self.session.add(acct)
            acct.name, acct.account_type, acct.book = ba.name, ba.account_type, book
            acct.broker, acct.base_currency, acct.external_id = broker.name, ba.base_currency, ba.external_id
            self.session.flush()
            res.accounts += 1
            if replace:
                self.session.execute(delete(models.Position).where(models.Position.account_id == ba.id))
                self.session.execute(delete(models.CashBalance).where(models.CashBalance.account_id == ba.id))
                self.session.flush()
            for cur, amt in ba.cash.items():
                self._set_cash(ba.id, cur, amt)
                res.cash_lines += 1

        merged: dict[tuple[str, str], list] = {}
        for h in broker.list_holdings():
            ticker = make_ticker(h.symbol, h.exchange)
            if self._ensure_instrument(ticker, h.name, h.asset_type):
                res.new_instruments.append(ticker)
            key = (h.account_id, ticker)
            if key in merged:  # same holding split over rows: combine at weighted cost
                q0, c0 = merged[key]
                merged[key] = [q0 + h.quantity, (q0 * c0 + h.quantity * h.avg_cost) / (q0 + h.quantity)]
            else:
                merged[key] = [h.quantity, h.avg_cost]
            if h.market_price and h.market_value and h.quantity:
                implied = h.quantity * h.market_price
                if abs(implied - h.market_value) > max(Decimal("1"), abs(h.market_value) * Decimal("0.01")):
                    res.flags.append(DataFlag("warn", "reconcile", ticker,
                                              f"{h.account_id}: {h.quantity} x {h.market_price} = {implied:.2f}, "
                                              f"but the file says market value {h.market_value}."))
        for (acct_id, ticker), (qty, cost) in merged.items():
            pos = self.session.scalars(select(models.Position).where(
                models.Position.account_id == acct_id, models.Position.ticker == ticker)).first()
            if pos is None:
                self.session.add(models.Position(account_id=acct_id, ticker=ticker, quantity=qty, avg_cost=cost))
            else:
                pos.quantity, pos.avg_cost = qty, cost
            res.positions += 1
        for w in getattr(broker, "warnings", []):
            res.flags.append(DataFlag("warn", "import", "import", w))
        models.audit(self.session, actor, "import_holdings", broker=broker.name, book=book,
                     accounts=res.accounts, positions=res.positions, new_instruments=res.new_instruments)
        self.session.flush()
        return res

    def _set_cash(self, account_id: str, currency: str, amount: Decimal) -> None:
        row = self.session.get(models.CashBalance, (account_id, currency))
        if row is None:
            self.session.add(models.CashBalance(account_id=account_id, currency=currency, amount=amount))
        else:
            row.amount = amount

    def record_deposit(self, account_id: str, currency: str, amount: Decimal, actor: str = "user") -> None:
        row = self.session.get(models.CashBalance, (account_id, currency))
        self._set_cash(account_id, currency, (row.amount if row else ZERO) + amount)
        models.audit(self.session, actor, "deposit", account_id=account_id, currency=currency, amount=amount)

    # prices ------------------------------------------------------------------
    def refresh_prices(self, prices: PriceService, tickers: list[str] | None = None) -> RefreshResult:
        res = RefreshResult()
        tickers = tickers or self.tracked_tickers()
        batch = prices.get_quotes(tickers) if tickers else None
        if batch:
            for t, q in batch.quotes.items():
                last = self.latest_price(t)
                if last and last.as_of == q.as_of and last.price == q.price and last.source == q.source:
                    continue  # nothing new (e.g. market closed)
                self.session.add(models.Price(ticker=t, price=q.price, prev_close=q.prev_close,
                                              currency=q.currency, as_of=q.as_of, source=q.source))
                res.updated.append(t)
            res.failed.update(batch.errors)
        currencies = {i.currency for i in self.session.scalars(select(models.Instrument))}
        currencies |= {c.currency for c in self.session.scalars(select(models.CashBalance))}
        for cur in sorted(currencies - {BASE_CURRENCY}):
            pair = f"{cur}{BASE_CURRENCY}"
            try:
                fxq = prices.get_fx(pair)
            except Exception as exc:  # noqa: BLE001 - recorded as a flag, never fatal
                res.fx[pair] = f"failed: {exc}"
                continue
            last = self.latest_fx(pair)
            if not (last and last.as_of == fxq.as_of and last.rate == fxq.rate):
                self.session.add(models.FxRate(pair=pair, rate=fxq.rate, as_of=fxq.as_of, source=fxq.source))
            res.fx[pair] = str(fxq.rate)
        self.session.flush()
        return res

    def record_headlines(self, items: list[NewsItem]) -> int:
        """Store news. 'Major' is a keyword placeholder until Market Watch (phase 2) owns it."""
        n = 0
        for it in items:
            exists = self.session.scalars(select(models.Headline.id).where(
                models.Headline.provider == it.provider, models.Headline.external_id == it.external_id)).first()
            if exists:
                continue
            text = f" {it.headline.lower()} "
            self.session.add(models.Headline(
                ticker=it.ticker, published_at=it.published_at, headline=it.headline, summary=it.summary,
                source=it.source, url=it.url, provider=it.provider, external_id=it.external_id,
                is_major=any(w in text for w in MAJOR_HEADLINE_WORDS),
            ))
            n += 1
        self.session.flush()
        return n

    # fills (called by the Trader's broker) ------------------------------------
    def apply_fill(self, account_id: str, ticker: str, side: str, quantity: Decimal, price: Decimal) -> Decimal:
        """Apply a fill to positions and cash. Returns realized P&L (instrument currency) for sells."""
        inst = self.session.get(models.Instrument, ticker) or None
        if inst is None:
            self._ensure_instrument(ticker)
            inst = self.session.get(models.Instrument, ticker)
        cur = inst.currency
        cash = self.session.get(models.CashBalance, (account_id, cur))
        cash_amt = cash.amount if cash else ZERO
        pos = self.session.scalars(select(models.Position).where(
            models.Position.account_id == account_id, models.Position.ticker == ticker)).first()
        notional = quantity * price
        realized = ZERO
        if side == "BUY":
            if notional > cash_amt:
                raise InsufficientFunds(f"needs {notional:.2f} {cur}, account has {cash_amt:.2f} {cur} "
                                        "(currency conversion is not simulated)")
            if pos is None:
                self.session.add(models.Position(account_id=account_id, ticker=ticker, quantity=quantity, avg_cost=price))
            else:
                new_qty = pos.quantity + quantity
                pos.avg_cost = (pos.quantity * pos.avg_cost + notional) / new_qty
                pos.quantity = new_qty
            self._set_cash(account_id, cur, cash_amt - notional)
        elif side == "SELL":
            held = pos.quantity if pos else ZERO
            if quantity > held:
                raise InsufficientFunds(f"can't sell {quantity} {ticker}; account holds {held}")
            realized = (price - pos.avg_cost) * quantity
            pos.quantity = held - quantity
            if pos.quantity == 0:
                self.session.delete(pos)
            self._set_cash(account_id, cur, cash_amt + notional)
        else:
            raise ValueError(side)
        models.audit(self.session, "bookkeeper", "apply_fill", account_id=account_id, ticker=ticker,
                     side=side, quantity=quantity, price=price, realized=realized)
        self.session.flush()
        return realized

    # paper book ----------------------------------------------------------------
    def clone_to_paper(self, actor: str = "user") -> list[str]:
        """(Re)create the paper book as a copy of the real one. Wipes paper positions and orders."""
        paper_ids = [a.id for a in self.accounts(models.BOOK_PAPER)]
        if paper_ids:
            self.session.execute(delete(models.Order).where(models.Order.account_id.in_(paper_ids)))
            self.session.execute(delete(models.Position).where(models.Position.account_id.in_(paper_ids)))
            self.session.execute(delete(models.CashBalance).where(models.CashBalance.account_id.in_(paper_ids)))
            self.session.execute(delete(models.Proposal).where(models.Proposal.account_id.in_(paper_ids)))
            self.session.execute(delete(models.Account).where(models.Account.id.in_(paper_ids)))
            self.session.flush()
        made = []
        for a in self.accounts(models.BOOK_REAL):
            pid = f"paper-{a.id}"
            self.session.add(models.Account(id=pid, name=f"{a.name} (paper)", account_type=a.account_type,
                                            book=models.BOOK_PAPER, broker="paper", base_currency=a.base_currency))
            self.session.flush()
            for p in self.session.scalars(select(models.Position).where(models.Position.account_id == a.id)):
                self.session.add(models.Position(account_id=pid, ticker=p.ticker, quantity=p.quantity, avg_cost=p.avg_cost))
            for c in self.session.scalars(select(models.CashBalance).where(models.CashBalance.account_id == a.id)):
                self.session.add(models.CashBalance(account_id=pid, currency=c.currency, amount=c.amount))
            made.append(pid)
        models.audit(self.session, actor, "paper_reset", accounts=made)
        self.session.flush()
        return made

    # plan ------------------------------------------------------------------------
    def set_target(self, bucket: str, target_pct: Decimal, band_pct: Decimal = Decimal("5"), actor: str = "user") -> None:
        if bucket not in BUCKETS:
            raise ValueError(f"unknown bucket {bucket}; one of {BUCKETS}")
        row = self.session.get(models.TargetAllocation, bucket)
        if row is None:
            self.session.add(models.TargetAllocation(bucket=bucket, target_pct=target_pct, band_pct=band_pct))
        else:
            row.target_pct, row.band_pct = target_pct, band_pct
        models.audit(self.session, actor, "set_target", bucket=bucket, target_pct=target_pct, band_pct=band_pct)

    def upsert_goal(self, goal_id: str, name: str, target_amount: Decimal, target_date: date,
                    account_ids: list[str], priority: int = 1, actor: str = "user",
                    monthly_contribution: Decimal | None = None) -> None:
        g = self.session.get(models.Goal, goal_id) or models.Goal(id=goal_id)
        g.name, g.target_amount, g.target_date, g.account_ids, g.priority = (
            name, target_amount, target_date, account_ids, priority)
        if monthly_contribution is not None:
            g.monthly_contribution = monthly_contribution
        self.session.add(g)
        models.audit(self.session, actor, "set_goal", goal=goal_id, target=target_amount, date=target_date)

    def set_goal_contribution(self, goal_id: str, monthly: Decimal, actor: str = "user") -> None:
        g = self.session.get(models.Goal, goal_id)
        if g is None:
            raise KeyError(f"no goal {goal_id}")
        g.monthly_contribution = monthly
        models.audit(self.session, actor, "set_goal_contribution", goal=goal_id, monthly=monthly)

    def set_assumption(self, key: str, value: Decimal, actor: str = "user") -> None:
        """Set an Advisor assumption: a bucket's expected return (%/yr) or 'min_trade_cad'."""
        if actor != "user":
            raise PermissionError("only you set the Advisor's assumptions")
        row = self.session.get(models.SystemFlag, ASSUMPTIONS_KEY)
        data = dict(row.value) if row else {}
        if key == "min_trade_cad":
            data["min_trade_cad"] = str(value)
        elif key in BUCKETS:
            if not Decimal(-50) < value < Decimal(50):
                raise ValueError("expected return must be a percent a year, e.g. 5.5")
            data["expected_return_pct"] = {**data.get("expected_return_pct", {}), key: str(value)}
        else:
            raise KeyError(f"unknown assumption {key}; a bucket ({', '.join(BUCKETS)}) or min_trade_cad")
        if row is None:
            self.session.add(models.SystemFlag(key=ASSUMPTIONS_KEY, value=data))
        else:
            row.value = data
        models.audit(self.session, actor, "set_assumption", key=key, value=value)
        self.session.flush()

    def add_watch(self, ticker: str, note: str = "", actor: str = "user") -> None:
        if self.session.get(models.WatchItem, ticker) is None:
            self.session.add(models.WatchItem(ticker=ticker, note=note))
        self._ensure_instrument(ticker)
        models.audit(self.session, actor, "watch_add", ticker=ticker)
        self.session.flush()

    def remove_watch(self, ticker: str, actor: str = "user") -> None:
        row = self.session.get(models.WatchItem, ticker)
        if row is not None:
            self.session.delete(row)
        models.audit(self.session, actor, "watch_remove", ticker=ticker)

    def record_events(self, items) -> int:
        n = 0
        for it in items:
            exists = self.session.scalars(select(models.CalendarEvent.id).where(
                models.CalendarEvent.provider == it.provider, models.CalendarEvent.external_id == it.external_id)).first()
            if exists:
                continue
            self.session.add(models.CalendarEvent(kind=it.kind, at=it.at, title=it.title, ticker=it.ticker,
                                                  country=it.country, detail=it.detail or {}, provider=it.provider,
                                                  external_id=it.external_id))
            n += 1
        self.session.flush()
        return n

    def update_headline(self, headline_id: int, *, relevant: bool, major: bool, category: str, why: str,
                        classified_by: str) -> models.Headline:
        """Market Watch's verdict on a headline. A major flag feeds the Risk Officer's 24-hour rule."""
        h = self.session.get(models.Headline, headline_id)
        h.relevant, h.is_major, h.category, h.why, h.classified_by = relevant, major, category, why, classified_by
        return h

    # snapshots & reports -----------------------------------------------------------
    def snapshot(self, book: str = models.BOOK_REAL) -> models.NetWorthSnapshot:
        view = self.portfolio(book)
        snap = models.NetWorthSnapshot(
            book=book, taken_at=self.now(), total_cad=view.total_cad, cash_cad=view.cash_cad,
            invested_cad=view.invested_cad,
            detail={"accounts": {a.id: str(money(a.total_cad)) for a in view.accounts},
                    "flags": [asdict(f) for f in view.flags]},
        )
        self.session.add(snap)
        self.session.flush()
        return snap

    def file_report(self, desk: ReportDesk, book: str = models.BOOK_REAL) -> AgentReport:
        """The Bookkeeper's daily report in the shared format. Facts only: stance is always NO VIEW."""
        view = self.portfolio(book)
        snap = self.snapshot(book)
        errors = [f for f in view.flags if f.severity == "error"]
        warns = [f for f in view.flags if f.severity == "warn"]
        finding = f"Net worth is ${money(view.total_cad):,} CAD, {pct(view.cash_cad, view.total_cad):.1f}% in cash"
        if errors or warns:
            finding += f"; {len(errors)} data error(s) and {len(warns)} warning(s) need attention."
        else:
            finding += "; the books reconcile with no data problems."
        if view.uses_sample_data:
            finding = finding[:-1] + " (on SAMPLE prices, not market data)."
        evidence = [Evidence(claim=f"Net worth snapshot {money(view.total_cad)} CAD",
                             source=f"record:net_worth_snapshots/{snap.id}")]
        evidence += [Evidence(claim=f.message, source=f"record:flags/{f.code}/{f.subject}") for f in errors + warns]
        report = AgentReport(
            agent="bookkeeper", as_of=view.as_of, subject="portfolio", finding=finding, stance=Stance.NO_VIEW,
            confidence=Decimal(1), urgency=Urgency.HIGH if errors else (Urgency.MEDIUM if warns else Urgency.LOW),
            evidence=evidence, wrong_if=None, next_check=view.as_of + timedelta(days=1),
        )
        return desk.submit(report)

    def count_rows(self, model) -> int:
        return self.session.scalar(select(func.count()).select_from(model)) or 0
