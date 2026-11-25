"""Database tables.

Who may write what (enforced by module boundaries, see README):
  - accounts, positions, cash, prices, fx, snapshots, goals, targets -> Bookkeeper
  - orders, proposal execution                                        -> Trader (+ PaperBroker)
  - rule settings, kill switch                                        -> Risk Officer's RuleBook (user-gated)
  - reports                                                           -> ReportDesk (any agent, validated)
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Boolean, Date, ForeignKey, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from atlas.db import Base, DecimalText, UTCDateTime, utcnow

BOOK_REAL = "real"
BOOK_PAPER = "paper"


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    # TFSA | RRSP | FHSA | NON_REGISTERED | CASH | OTHER
    account_type: Mapped[str] = mapped_column(String(32))
    book: Mapped[str] = mapped_column(String(16), default=BOOK_REAL)  # real | paper
    broker: Mapped[str] = mapped_column(String(32))  # manual | paper | snaptrade
    base_currency: Mapped[str] = mapped_column(String(3), default="CAD")
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Instrument(Base):
    __tablename__ = "instruments"

    ticker: Mapped[str] = mapped_column(String(32), primary_key=True)  # "XEQT.TO", "AAPL.US"
    symbol: Mapped[str] = mapped_column(String(24))
    exchange: Mapped[str] = mapped_column(String(8))  # EODHD exchange code: TO, V, NEO, US
    name: Mapped[str] = mapped_column(String(160), default="")
    currency: Mapped[str] = mapped_column(String(3))
    asset_type: Mapped[str] = mapped_column(String(16), default="stock")  # stock | etf
    sector: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Look-through mix, e.g. {"ca_equity": 0.25, "us_equity": 0.45, "intl_equity": 0.30}
    allocation: Mapped[dict] = mapped_column(JSON, default=dict)
    mer_pct: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    classified: Mapped[bool] = mapped_column(Boolean, default=True)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("account_id", "ticker"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"))
    ticker: Mapped[str] = mapped_column(ForeignKey("instruments.ticker"))
    quantity: Mapped[Decimal] = mapped_column(DecimalText)
    avg_cost: Mapped[Decimal] = mapped_column(DecimalText)  # per unit, instrument currency
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class CashBalance(Base):
    __tablename__ = "cash_balances"

    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    amount: Mapped[Decimal] = mapped_column(DecimalText)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Price(Base):
    """Price cache. Append-only; the latest as_of per ticker wins."""

    __tablename__ = "prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    price: Mapped[Decimal] = mapped_column(DecimalText)
    prev_close: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    currency: Mapped[str] = mapped_column(String(3))
    as_of: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class FxRate(Base):
    __tablename__ = "fx_rates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pair: Mapped[str] = mapped_column(String(6), index=True)  # "USDCAD" = CAD per 1 USD
    rate: Mapped[Decimal] = mapped_column(DecimalText)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    source: Mapped[str] = mapped_column(String(32))
    fetched_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Headline(Base):
    __tablename__ = "headlines"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(32), index=True)
    published_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    headline: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(128), default="")
    url: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(128))
    is_major: Mapped[bool] = mapped_column(Boolean, default=False)
    # Set by Market Watch. relevant=False means "doesn't touch your money": kept, but not reported.
    relevant: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    why: Mapped[str | None] = mapped_column(Text, nullable=True)
    classified_by: Mapped[str | None] = mapped_column(String(32), nullable=True)


class WatchItem(Base):
    """Something you watch or plan for but don't own. Market Watch keeps news on these too."""

    __tablename__ = "watchlist"

    ticker: Mapped[str] = mapped_column(String(32), primary_key=True)
    note: Mapped[str] = mapped_column(Text, default="")
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CalendarEvent(Base):
    """Earnings dates, economic releases and central-bank decisions."""

    __tablename__ = "calendar_events"
    __table_args__ = (UniqueConstraint("provider", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(16))  # earnings | economic | policy
    at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    title: Mapped[str] = mapped_column(Text)
    ticker: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    provider: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(160))


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    target_amount: Mapped[Decimal] = mapped_column(DecimalText)
    target_date: Mapped[date] = mapped_column(Date)
    priority: Mapped[int] = mapped_column(Integer, default=1)
    account_ids: Mapped[list] = mapped_column(JSON, default=list)
    monthly_contribution: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)


class TargetAllocation(Base):
    __tablename__ = "target_allocation"

    bucket: Mapped[str] = mapped_column(String(32), primary_key=True)
    target_pct: Mapped[Decimal] = mapped_column(DecimalText)
    band_pct: Mapped[Decimal] = mapped_column(DecimalText, default=Decimal("5"))


class RuleSetting(Base):
    __tablename__ = "rule_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Decimal] = mapped_column(DecimalText)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_by: Mapped[str] = mapped_column(String(32), default="system")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class SystemFlag(Base):
    __tablename__ = "system_flags"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Report(Base):
    """A specialist report in the shared format (see atlas.reports)."""

    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(String(48), index=True)
    as_of: Mapped[datetime] = mapped_column(UTCDateTime)
    subject: Mapped[str] = mapped_column(String(64), index=True)
    finding: Mapped[str] = mapped_column(Text)
    stance: Mapped[str] = mapped_column(String(8))
    confidence: Mapped[Decimal] = mapped_column(DecimalText)
    urgency: Mapped[str] = mapped_column(String(8))
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    wrong_if: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_check: Mapped[datetime] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class Proposal(Base):
    """A trade idea on its way to a decision card: proposed -> (blocked | approved | dismissed) -> executed."""

    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    ticker: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))  # BUY | SELL
    quantity: Mapped[Decimal] = mapped_column(DecimalText)
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    order_type: Mapped[str] = mapped_column(String(8), default="LIMIT")
    rationale: Mapped[str] = mapped_column(Text, default="")
    proposed_by: Mapped[str] = mapped_column(String(48), default="user")
    report_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(12), default="proposed")
    risk_result: Mapped[dict] = mapped_column(JSON, default=dict)
    approval_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A multi-leg plan (e.g. an Advisor rebalance): legs share group_id and run in `sequence` order.
    group_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    sequence: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    ticker: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[Decimal] = mapped_column(DecimalText)
    order_type: Mapped[str] = mapped_column(String(8), default="LIMIT")
    limit_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    # open | filled | cancelled | rejected | expired
    status: Mapped[str] = mapped_column(String(12), default="open")
    broker: Mapped[str] = mapped_column(String(32))
    proposal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fill_price: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    fill_quantity: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(DecimalText, nullable=True)  # instrument ccy
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    filled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class NetWorthSnapshot(Base):
    __tablename__ = "net_worth_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    book: Mapped[str] = mapped_column(String(16), default=BOOK_REAL)
    taken_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    total_cad: Mapped[Decimal] = mapped_column(DecimalText)
    cash_cad: Mapped[Decimal] = mapped_column(DecimalText)
    invested_cad: Mapped[Decimal] = mapped_column(DecimalText)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


class AuditLog(Base):
    """Decision log: every rule change, kill-switch toggle, proposal, approval and order."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(48))
    action: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)


def audit(session, actor: str, action: str, **detail) -> None:
    session.add(AuditLog(actor=actor, action=action, detail=_jsonable(detail)))


def _jsonable(obj):
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj
