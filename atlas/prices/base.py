"""Market-data adapter interfaces.

Every data source sits behind one of these so a placeholder can stand in until
the real connection is ready, and swapping one in changes no agent code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable, Protocol, runtime_checkable


class AdapterError(RuntimeError):
    """A data source failed. Messages never include API keys."""


@dataclass(frozen=True)
class Quote:
    ticker: str
    price: Decimal
    currency: str
    as_of: datetime
    source: str
    prev_close: Decimal | None = None


@dataclass(frozen=True)
class Bar:
    day: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjusted_close: Decimal
    volume: int


@dataclass(frozen=True)
class NewsItem:
    ticker: str
    published_at: datetime
    headline: str
    summary: str
    source: str
    url: str
    provider: str
    external_id: str


@dataclass(frozen=True)
class FxQuote:
    pair: str  # "USDCAD" = CAD per 1 USD
    rate: Decimal
    as_of: datetime
    source: str


@runtime_checkable
class QuoteAdapter(Protocol):
    name: str

    def supports(self, ticker: str) -> bool: ...

    def get_quotes(self, tickers: Iterable[str]) -> dict[str, Quote]: ...


@runtime_checkable
class NewsAdapter(Protocol):
    name: str

    def supports(self, ticker: str) -> bool: ...

    def get_news(self, ticker: str, since: date, until: date) -> list[NewsItem]: ...


@runtime_checkable
class FxAdapter(Protocol):
    name: str

    def get_rate(self, pair: str) -> FxQuote: ...


def to_decimal(value) -> Decimal | None:
    """Parse a numeric field from a data feed; 'NA', '', None, 0-as-missing handled by caller."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip().upper() in {"", "NA", "N/A", "NULL", "NONE", "-"}:
        return None
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


_SECRET_PARAM = re.compile(r"(api_token|token|apikey|api_key)=[^&\s]+", re.I)


def redact(text: str) -> str:
    return _SECRET_PARAM.sub(r"\1=***", text)
