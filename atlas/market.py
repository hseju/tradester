"""Tickers, exchanges and market hours.

Canonical tickers use the EODHD form SYMBOL.EXCHANGE: "XEQT.TO" (TSX),
"SHOP.V" (TSX Venture), "VFV.TO", "AAPL.US". US-listed names use ".US"
regardless of NYSE/Nasdaq.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from atlas.config import MARKET_TZ

ET = ZoneInfo(MARKET_TZ)

# Exchange names as brokers and spreadsheets write them -> EODHD code
EXCHANGE_ALIASES = {
    "TO": "TO", "TSX": "TO", "TOR": "TO", "XTSE": "TO", "TSE": "TO", "CA": "TO",
    "V": "V", "TSXV": "V", "XTSX": "V", "TSX-V": "V", "TSXV.": "V",
    "NEO": "NEO", "CBOE CANADA": "NEO", "NE": "NEO", "AEQUITAS": "NEO",
    "CN": "CN", "CSE": "CN", "XCNQ": "CN",
    "US": "US", "NYSE": "US", "NASDAQ": "US", "NYSE ARCA": "US", "ARCA": "US", "AMEX": "US",
    "NYSEARCA": "US", "XNYS": "US", "XNAS": "US", "ARCX": "US", "BATS": "US", "CBOE": "US",
    "NYSE AMERICAN": "US", "NMS": "US",
}

EXCHANGE_CURRENCY = {"TO": "CAD", "V": "CAD", "NEO": "CAD", "CN": "CAD", "US": "USD"}


def normalize_exchange(exchange: str) -> str:
    code = EXCHANGE_ALIASES.get(exchange.strip().upper())
    if not code:
        raise ValueError(f"unknown exchange {exchange!r}")
    return code


def make_ticker(symbol: str, exchange: str) -> str:
    return f"{symbol.strip().upper()}.{normalize_exchange(exchange)}"


def parse_ticker(ticker: str) -> tuple[str, str]:
    """'XEQT.TO' -> ('XEQT', 'TO'). A bare symbol is treated as US-listed.

    Symbols may themselves contain a dot (e.g. 'BRK.B.US', 'BAM.A.TO'), so we
    split on the last dot and only accept a known exchange code after it.
    """
    t = ticker.strip().upper()
    if "." in t:
        sym, _, exch = t.rpartition(".")
        if exch in EXCHANGE_CURRENCY:
            return sym, exch
    return t, "US"


def normalize_ticker(ticker: str) -> str:
    sym, exch = parse_ticker(ticker)
    return f"{sym}.{exch}"


def currency_for(exchange: str) -> str:
    return EXCHANGE_CURRENCY.get(exchange, "CAD")


def is_market_open(now: datetime) -> bool:
    """Regular session for TSX and NYSE/Nasdaq: 9:30-16:00 ET, Mon-Fri.

    Exchange holidays are not modelled in phase 1; on a holiday the price job
    simply refreshes to the same close.
    """
    local = now.astimezone(ET)
    if local.weekday() >= 5:
        return False
    return time(9, 30) <= local.time() < time(16, 0)
