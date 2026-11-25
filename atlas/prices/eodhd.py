"""EODHD adapter: TSX + US quotes, daily history, news, FX and fundamentals.

Docs: https://eodhd.com/financial-apis/  (tickers are SYMBOL.EXCHANGE, e.g. XEQT.TO, AAPL.US)
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Iterable

import httpx2 as httpx

from atlas.market import ET, currency_for, parse_ticker
from atlas.prices.base import AdapterError, Bar, FxQuote, NewsItem, Quote, redact, to_decimal

BASE_URL = "https://eodhd.com/api"
BATCH = 15  # tickers per real-time request


class EODHDAdapter:
    name = "eodhd"

    def __init__(self, api_key: str, client: httpx.Client | None = None, timeout: float = 15.0):
        if not api_key:
            raise ValueError("EODHD API key required")
        self._key = api_key
        self._client = client or httpx.Client(timeout=timeout)

    # -- plumbing -----------------------------------------------------------
    def _get(self, path: str, **params):
        params = {**params, "api_token": self._key, "fmt": "json"}
        try:
            resp = self._client.get(f"{BASE_URL}{path}", params=params)
        except httpx.HTTPError as exc:
            raise AdapterError(f"eodhd request failed: {redact(str(exc))}") from None
        if resp.status_code != 200:
            raise AdapterError(f"eodhd {path} -> HTTP {resp.status_code}: {redact(resp.text[:200])}")
        try:
            return resp.json()
        except ValueError:
            raise AdapterError(f"eodhd {path} returned non-JSON: {redact(resp.text[:200])}") from None

    # -- quotes -------------------------------------------------------------
    def supports(self, ticker: str) -> bool:
        return parse_ticker(ticker)[1] in {"TO", "V", "NEO", "CN", "US"}

    def get_quotes(self, tickers: Iterable[str]) -> dict[str, Quote]:
        tickers = list(dict.fromkeys(tickers))
        out: dict[str, Quote] = {}
        for i in range(0, len(tickers), BATCH):
            chunk = tickers[i : i + BATCH]
            params = {"s": ",".join(chunk[1:])} if len(chunk) > 1 else {}
            data = self._get(f"/real-time/{chunk[0]}", **params)
            rows = data if isinstance(data, list) else [data]
            for row in rows:
                q = self._parse_quote(row)
                if q:
                    out[q.ticker] = q
        return out

    def _parse_quote(self, row: dict) -> Quote | None:
        code = str(row.get("code", "")).upper()
        price = to_decimal(row.get("close"))
        ts = row.get("timestamp")
        if not code or price is None or price <= 0 or not isinstance(ts, (int, float)):
            return None
        _, exch = parse_ticker(code)
        return Quote(
            ticker=code,
            price=price,
            currency=currency_for(exch),
            as_of=datetime.fromtimestamp(ts, tz=timezone.utc),
            source=self.name,
            prev_close=to_decimal(row.get("previousClose")),
        )

    # -- history ------------------------------------------------------------
    def get_eod(self, ticker: str, start: date, end: date) -> list[Bar]:
        rows = self._get(f"/eod/{ticker}", **{"from": start.isoformat(), "to": end.isoformat(), "period": "d"})
        bars = []
        for r in rows or []:
            close = to_decimal(r.get("close"))
            if close is None:
                continue
            bars.append(
                Bar(
                    day=date.fromisoformat(r["date"]),
                    open=to_decimal(r.get("open")) or close,
                    high=to_decimal(r.get("high")) or close,
                    low=to_decimal(r.get("low")) or close,
                    close=close,
                    adjusted_close=to_decimal(r.get("adjusted_close")) or close,
                    volume=int(r.get("volume") or 0),
                )
            )
        return bars

    # -- news ---------------------------------------------------------------
    def get_news(self, ticker: str, since: date, until: date) -> list[NewsItem]:
        rows = self._get("/news", s=ticker, **{"from": since.isoformat(), "to": until.isoformat()}, limit=50)
        items = []
        for r in rows or []:
            try:
                published = datetime.fromisoformat(str(r["date"]).replace("Z", "+00:00"))
            except (KeyError, ValueError):
                continue
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            link = r.get("link") or ""
            items.append(
                NewsItem(
                    ticker=ticker,
                    published_at=published,
                    headline=(r.get("title") or "").strip(),
                    summary=(r.get("content") or "")[:1000],
                    source="EODHD",
                    url=link,
                    provider=self.name,
                    external_id=link or f"{ticker}:{published.isoformat()}",
                )
            )
        return items

    # -- fx -----------------------------------------------------------------
    def get_rate(self, pair: str) -> FxQuote:
        row = self._get(f"/real-time/{pair.upper()}.FOREX")
        if isinstance(row, list):
            row = row[0] if row else {}
        if not isinstance(row, dict):
            raise AdapterError(f"eodhd returned an unexpected shape for {pair}")
        rate, ts = to_decimal(row.get("close")), row.get("timestamp")
        if rate is None or not isinstance(ts, (int, float)):
            raise AdapterError(f"eodhd returned no rate for {pair}")
        return FxQuote(pair=pair.upper(), rate=rate, as_of=datetime.fromtimestamp(ts, tz=timezone.utc), source=self.name)

    # -- fundamentals (used by Value/Filings Research in later phases) ------
    def get_fundamentals(self, ticker: str) -> dict:
        return self._get(f"/fundamentals/{ticker}")


def market_close_utc(day: date) -> datetime:
    return datetime.combine(day, time(16, 0), tzinfo=ET).astimezone(timezone.utc)
