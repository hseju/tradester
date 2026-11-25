"""PaperBroker: simulated fills at real prices, for the paper-trading record.

Paper accounts live in the same database as the real ones, with book="paper"
(see Bookkeeper.clone_to_paper). Orders are limit-only DAY orders:

  - BUY fills when the market price is at or below the limit,
    at market + slippage, never above the limit.
  - SELL fills when the market price is at or above the limit,
    at market - slippage, never below the limit.
  - Unfilled orders are re-checked on each price refresh and expire at the close.

Fills are written to the books by the Bookkeeper (cash and positions).
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from atlas import models
from atlas.brokers.base import (
    BrokerAccount,
    BrokerAdapter,
    BrokerError,
    BrokerHolding,
    BrokerOrder,
    OrderRequest,
)
from atlas.db import utcnow
from atlas.market import ET
from atlas.prices.service import PriceService


class InsufficientFunds(BrokerError):
    pass


def _to_broker_order(o: models.Order) -> BrokerOrder:
    return BrokerOrder(
        id=o.id, account_id=o.account_id, ticker=o.ticker, side=o.side, quantity=o.quantity,
        order_type=o.order_type, limit_price=o.limit_price, status=o.status, created_at=o.created_at,
        filled_at=o.filled_at, fill_price=o.fill_price, fill_quantity=o.fill_quantity, note=o.note,
    )


class PaperBroker(BrokerAdapter):
    name = "paper"
    can_trade = True

    def __init__(self, session: Session, prices: PriceService, bookkeeper, slippage_bps: int = 5, now=utcnow):
        self.session = session
        self.prices = prices
        self.bookkeeper = bookkeeper
        self.slippage = Decimal(slippage_bps) / Decimal(10_000)
        self._now = now

    # -- reads ----------------------------------------------------------------
    def _paper_accounts(self) -> list[models.Account]:
        return list(self.session.scalars(select(models.Account).where(models.Account.book == models.BOOK_PAPER)))

    def list_accounts(self) -> list[BrokerAccount]:
        out = []
        for a in self._paper_accounts():
            cash = {c.currency: c.amount for c in self.session.scalars(
                select(models.CashBalance).where(models.CashBalance.account_id == a.id))}
            out.append(BrokerAccount(a.id, a.name, a.account_type, a.base_currency, cash))
        return out

    def list_holdings(self, account_id: str | None = None) -> list[BrokerHolding]:
        q = (
            select(models.Position, models.Instrument)
            .join(models.Instrument, models.Position.ticker == models.Instrument.ticker)
            .join(models.Account, models.Position.account_id == models.Account.id)
            .where(models.Account.book == models.BOOK_PAPER)
        )
        if account_id:
            q = q.where(models.Position.account_id == account_id)
        return [
            BrokerHolding(p.account_id, i.symbol, i.exchange, p.quantity, p.avg_cost, i.currency, i.name, i.asset_type)
            for p, i in self.session.execute(q)
        ]

    def list_orders(self, account_id: str | None = None, since: datetime | None = None) -> list[BrokerOrder]:
        q = select(models.Order).where(models.Order.broker == self.name).order_by(models.Order.created_at.desc())
        if account_id:
            q = q.where(models.Order.account_id == account_id)
        if since:
            q = q.where(models.Order.created_at >= since)
        return [_to_broker_order(o) for o in self.session.scalars(q)]

    # -- orders ---------------------------------------------------------------
    def place_order(self, request: OrderRequest) -> BrokerOrder:
        acct = self.session.get(models.Account, request.account_id)
        if acct is None or acct.book != models.BOOK_PAPER:
            raise BrokerError(f"{request.account_id} is not a paper account")
        if request.order_type != "LIMIT" or not request.limit_price or request.limit_price <= 0:
            raise BrokerError("paper broker accepts limit orders only")
        if request.quantity <= 0:
            raise BrokerError("quantity must be positive")
        if request.side not in {"BUY", "SELL"}:
            raise BrokerError("side must be BUY or SELL")
        order = models.Order(
            id=f"paper-{uuid.uuid4().hex[:12]}",
            account_id=request.account_id,
            ticker=request.ticker,
            side=request.side,
            quantity=request.quantity,
            order_type="LIMIT",
            limit_price=request.limit_price,
            status="open",
            broker=self.name,
            proposal_id=request.proposal_id,
            created_at=self._now(),
        )
        self.session.add(order)
        self.session.flush()
        self._try_fill(order)
        models.audit(self.session, "trader", "paper_order", order_id=order.id, status=order.status,
                     ticker=order.ticker, side=order.side, quantity=order.quantity, fill_price=order.fill_price)
        return _to_broker_order(order)

    def _try_fill(self, order: models.Order) -> None:
        batch = self.prices.get_quotes([order.ticker])
        quote = batch.quotes.get(order.ticker)
        if quote is None:
            order.note = f"no price: {batch.errors.get(order.ticker, 'unknown')}"
            return
        px, limit = quote.price, order.limit_price
        if order.side == "BUY":
            if px > limit:
                order.note = f"waiting: market {px} above limit {limit}"
                return
            fill = min(limit, px * (1 + self.slippage))
        else:
            if px < limit:
                order.note = f"waiting: market {px} below limit {limit}"
                return
            fill = max(limit, px * (1 - self.slippage))
        fill = fill.quantize(Decimal("0.0001"))
        try:
            realized = self.bookkeeper.apply_fill(order.account_id, order.ticker, order.side, order.quantity, fill)
        except InsufficientFunds as exc:
            order.status, order.note = "rejected", str(exc)
            return
        order.status = "filled"
        order.fill_price = fill
        order.fill_quantity = order.quantity
        order.filled_at = self._now()
        order.realized_pnl = realized
        order.note = f"filled vs market {px} ({quote.source})"

    def process_open_orders(self) -> list[BrokerOrder]:
        """Re-check open orders against fresh prices; expire DAY orders after the close."""
        now = self._now()
        changed = []
        for order in self.session.scalars(
            select(models.Order).where(models.Order.broker == self.name, models.Order.status == "open")
        ):
            if now >= session_close(order.created_at):
                order.status, order.note = "expired", "DAY order expired at the close"
            else:
                self._try_fill(order)
            if order.status != "open":
                changed.append(_to_broker_order(order))
        return changed

    def cancel(self, order_id: str) -> BrokerOrder:
        order = self.session.get(models.Order, order_id)
        if order is None or order.broker != self.name:
            raise BrokerError(f"no paper order {order_id}")
        if order.status == "open":
            order.status, order.note = "cancelled", "cancelled by user"
        return _to_broker_order(order)


def session_close(placed_at: datetime) -> datetime:
    """When a DAY order placed at `placed_at` expires: that day's 16:00 ET close,
    or the next weekday's close if placed after hours or on a weekend."""
    local = placed_at.astimezone(ET)
    day = local.date()
    if local.time() >= time(16, 0) or local.weekday() >= 5:
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    return datetime.combine(day, time(16, 0), tzinfo=ET).astimezone(timezone.utc)
