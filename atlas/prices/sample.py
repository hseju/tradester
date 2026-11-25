"""Placeholder market data, used when no market-data keys are configured.

Prices are fixed, illustrative SAMPLE values - not real quotes. Every price
row written from here carries source="sample" and the Bookkeeper flags it.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Iterable

from atlas.market import currency_for, normalize_ticker, parse_ticker
from atlas.market import ET
from atlas.prices.base import CalendarItem, FxQuote, NewsItem, Quote

SAMPLE_PRICES: dict[str, Decimal] = {
    # Canadian-listed ETFs (CAD)
    "XEQT.TO": Decimal("36.00"),
    "VFV.TO": Decimal("160.00"),
    "ZAG.TO": Decimal("14.00"),
    "XEF.TO": Decimal("42.00"),
    "XIC.TO": Decimal("44.00"),
    "VEQT.TO": Decimal("48.00"),
    "XBB.TO": Decimal("28.50"),
    # Canadian stock (CAD)
    "RY.TO": Decimal("190.00"),
    # US-listed (USD)
    "VOO.US": Decimal("620.00"),
    "AAPL.US": Decimal("270.00"),
    "NVDA.US": Decimal("190.00"),
}
# Previous close, to give a believable "today" move. NVDA ran up.
SAMPLE_PREV_CLOSE_PCT: dict[str, Decimal] = {"NVDA.US": Decimal("0.975")}
SAMPLE_FX = {"USDCAD": Decimal("1.4000")}


class SampleAdapter:
    name = "sample"

    def __init__(self, prices: dict[str, Decimal] | None = None, now=lambda: datetime.now(timezone.utc)):
        self.prices = dict(SAMPLE_PRICES if prices is None else prices)
        self._now = now

    def supports(self, ticker: str) -> bool:
        return normalize_ticker(ticker) in self.prices

    def get_quotes(self, tickers: Iterable[str]) -> dict[str, Quote]:
        out = {}
        for t in tickers:
            t = normalize_ticker(t)
            if t not in self.prices:
                continue
            p = self.prices[t]
            prev = (p * SAMPLE_PREV_CLOSE_PCT.get(t, Decimal("0.995"))).quantize(Decimal("0.01"))
            out[t] = Quote(t, p, currency_for(parse_ticker(t)[1]), self._now(), self.name, prev)
        return out

    def get_news(self, ticker: str, since: date, until: date) -> list[NewsItem]:
        """A few illustrative headlines so Market Watch has something to read offline."""
        now = self._now()
        canned = {
            "NVDA.US": [("[SAMPLE] NVIDIA shares extend rally as chip stocks climb", 3)],
            "AAPL.US": [("[SAMPLE] Apple to report quarterly results next week", 20)],
            "RY.TO": [("[SAMPLE] Canadian banks steady ahead of quarterly reports", 30)],
            "XEQT.TO": [("[SAMPLE] Celebrity chef opens new restaurant downtown", 5)],  # irrelevant: filtered out
        }
        out = []
        for i, (headline, hours_ago) in enumerate(canned.get(normalize_ticker(ticker), [])):
            at = now - timedelta(hours=hours_ago)
            if since <= at.date() <= until:
                out.append(NewsItem(normalize_ticker(ticker), at, headline, "", "SAMPLE", "", self.name,
                                    f"sample:{ticker}:{i}:{at.date()}"))
        return out

    def get_earnings(self, ticker: str, since: date, until: date) -> list[CalendarItem]:
        days = {"AAPL.US": 6, "RY.TO": 9}.get(normalize_ticker(ticker))
        if days is None:
            return []
        day = self._now().astimezone(ET).date() + timedelta(days=days)
        if not since <= day <= until:
            return []
        return [CalendarItem("earnings", datetime.combine(day, time(16), tzinfo=ET).astimezone(timezone.utc),
                             f"[SAMPLE] {normalize_ticker(ticker)} earnings", self.name,
                             f"sample:earn:{ticker}:{day}", ticker=normalize_ticker(ticker))]

    def get_economic_events(self, country: str, since: date, until: date) -> list[CalendarItem]:
        day = self._now().astimezone(ET).date() + timedelta(days=2)
        title = {"CA": "[SAMPLE] CA CPI (yoy)", "US": "[SAMPLE] US Nonfarm Payrolls"}.get(country.upper())
        if title is None or not since <= day <= until:
            return []
        return [CalendarItem("economic", datetime.combine(day, time(8, 30), tzinfo=ET).astimezone(timezone.utc),
                             title, self.name, f"sample:econ:{country}:{day}", country=country.upper())]

    def get_policy_rate(self) -> tuple[Decimal, date]:
        return Decimal("2.25"), self._now().date()

    def get_rate(self, pair: str) -> FxQuote:
        return FxQuote(pair.upper(), SAMPLE_FX[pair.upper()], self._now(), self.name)
