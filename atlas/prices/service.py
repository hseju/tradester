"""Routes each request to the right data source, with fallbacks.

  US quotes  -> Finnhub (live), then EODHD
  TSX quotes -> EODHD
  News       -> Finnhub for US names, EODHD for Canadian names
  FX         -> Bank of Canada (official daily), then EODHD

If no market-data key is configured at all (or ATLAS_PRICE_SOURCE=sample),
everything comes from the SampleAdapter. When real sources are configured we
never silently fall back to sample prices: a failed fetch leaves the last
cached price in place, and the Bookkeeper flags it as it goes stale.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable

from atlas.config import Settings
from atlas.prices.base import AdapterError, FxAdapter, FxQuote, NewsAdapter, NewsItem, Quote, QuoteAdapter

log = logging.getLogger(__name__)


@dataclass
class QuoteBatch:
    quotes: dict[str, Quote] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)  # ticker -> reason


class PriceService:
    def __init__(
        self,
        quote_adapters: list[QuoteAdapter],
        news_adapters: list[NewsAdapter],
        fx_adapters: list[FxAdapter],
    ):
        self.quote_adapters = quote_adapters
        self.news_adapters = news_adapters
        self.fx_adapters = fx_adapters

    @property
    def is_sample(self) -> bool:
        return all(a.name == "sample" for a in self.quote_adapters)

    @classmethod
    def from_settings(cls, settings: Settings, client=None) -> "PriceService":
        from atlas.prices.sample import SampleAdapter

        has_keys = bool(settings.eodhd_api_key or settings.finnhub_api_key)
        if settings.price_source == "sample" or not has_keys:
            sample = SampleAdapter()
            return cls([sample], [sample], [sample])

        from atlas.prices.bankofcanada import BankOfCanadaFx
        from atlas.prices.eodhd import EODHDAdapter
        from atlas.prices.finnhub import FinnhubAdapter

        kw = {"client": client, "timeout": settings.http_timeout}
        quotes: list = []
        news: list = []
        fx: list = [BankOfCanadaFx(**kw)]
        finnhub = FinnhubAdapter(settings.finnhub_api_key, **kw) if settings.finnhub_api_key else None
        eodhd = EODHDAdapter(settings.eodhd_api_key, **kw) if settings.eodhd_api_key else None
        if finnhub:
            quotes.append(finnhub)
            news.append(finnhub)
        if eodhd:
            quotes.append(eodhd)
            news.append(eodhd)
            fx.append(eodhd)
        return cls(quotes, news, fx)

    def get_quotes(self, tickers: Iterable[str]) -> QuoteBatch:
        batch = QuoteBatch()
        pending = list(dict.fromkeys(tickers))
        for adapter in self.quote_adapters:
            mine = [t for t in pending if adapter.supports(t)]
            if not mine:
                continue
            try:
                got = adapter.get_quotes(mine)
            except AdapterError as exc:
                log.warning("%s failed: %s", adapter.name, exc)
                for t in mine:
                    batch.errors[t] = str(exc)
                continue
            batch.quotes.update(got)
            for t in mine:
                if t in got:
                    batch.errors.pop(t, None)
                else:
                    batch.errors.setdefault(t, f"{adapter.name} returned no quote")
            pending = [t for t in pending if t not in got]
        for t in pending:
            batch.errors.setdefault(t, "no configured data source covers this ticker")
        return batch

    def get_news(self, ticker: str, since: date, until: date) -> list[NewsItem]:
        for adapter in self.news_adapters:
            if not adapter.supports(ticker):
                continue
            try:
                return adapter.get_news(ticker, since, until)
            except AdapterError as exc:
                log.warning("%s news failed for %s: %s", adapter.name, ticker, exc)
        return []

    def get_fx(self, pair: str) -> FxQuote:
        errors = []
        for adapter in self.fx_adapters:
            try:
                return adapter.get_rate(pair)
            except AdapterError as exc:
                errors.append(f"{adapter.name}: {exc}")
        raise AdapterError(f"no FX rate for {pair}: " + "; ".join(errors))
