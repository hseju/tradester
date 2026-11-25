"""The broker slot.

The app asks any broker for the same four things: list accounts, list
holdings, list recent orders, and place an order. Three versions fill it:
ManualBroker (spreadsheet), PaperBroker (simulated fills) and, later,
SnapTradeBroker (the real Wealthsimple connection).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

ACCOUNT_TYPES = {"TFSA", "RRSP", "FHSA", "RESP", "LIRA", "RRIF", "NON_REGISTERED", "CASH", "OTHER"}


class BrokerError(RuntimeError):
    pass


class OrderNotSupported(BrokerError):
    """This broker can't place orders (e.g. a spreadsheet, or trading not yet enabled)."""


@dataclass(frozen=True)
class BrokerAccount:
    id: str
    name: str
    account_type: str
    base_currency: str = "CAD"
    cash: dict[str, Decimal] = field(default_factory=dict)
    external_id: str | None = None


@dataclass(frozen=True)
class BrokerHolding:
    account_id: str
    symbol: str
    exchange: str
    quantity: Decimal
    avg_cost: Decimal  # per unit, in `currency`
    currency: str
    name: str = ""
    asset_type: str | None = None
    # What the broker/spreadsheet says it's worth, for reconciliation (optional)
    market_price: Decimal | None = None
    market_value: Decimal | None = None


@dataclass(frozen=True)
class OrderRequest:
    account_id: str
    ticker: str
    side: str  # BUY | SELL
    quantity: Decimal
    limit_price: Decimal | None
    order_type: str = "LIMIT"
    proposal_id: int | None = None


@dataclass(frozen=True)
class BrokerOrder:
    id: str
    account_id: str
    ticker: str
    side: str
    quantity: Decimal
    order_type: str
    limit_price: Decimal | None
    status: str  # open | filled | cancelled | rejected | expired
    created_at: datetime
    filled_at: datetime | None = None
    fill_price: Decimal | None = None
    fill_quantity: Decimal | None = None
    note: str = ""


class BrokerAdapter(ABC):
    name: str = "broker"
    can_trade: bool = False

    @abstractmethod
    def list_accounts(self) -> list[BrokerAccount]: ...

    @abstractmethod
    def list_holdings(self, account_id: str | None = None) -> list[BrokerHolding]: ...

    @abstractmethod
    def list_orders(self, account_id: str | None = None, since: datetime | None = None) -> list[BrokerOrder]: ...

    @abstractmethod
    def place_order(self, request: OrderRequest) -> BrokerOrder: ...
