"""Finnhub adapter: US live quotes and company news (free tier, personal use).

Docs: https://finnhub.io/docs/api  - the free tier covers US listings only.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable

import httpx2 as httpx

from atlas.market import parse_ticker
from atlas.prices.base import AdapterError, CalendarItem, NewsItem, Quote, redact, to_decimal

BASE_URL = "https://finnhub.io/api/v1"


class FinnhubAdapter:
    name = "finnhub"

    def __init__(self, api_key: str, client: httpx.Client | None = None, timeout: float = 15.0):
        if not api_key:
            raise ValueError("Finnhub API key required")
        self._client = client or httpx.Client(timeout=timeout)
        self._headers = {"X-Finnhub-Token": api_key}  # header, so keys never land in URLs/logs

    def _get(self, path: str, **params):
        try:
            resp = self._client.get(f"{BASE_URL}{path}", params=params, headers=self._headers)
        except httpx.HTTPError as exc:
            raise AdapterError(f"finnhub request failed: {redact(str(exc))}") from None
        if resp.status_code == 429:
            raise AdapterError("finnhub rate limit hit (free tier: 60 calls/min)")
        if resp.status_code != 200:
            raise AdapterError(f"finnhub {path} -> HTTP {resp.status_code}: {redact(resp.text[:200])}")
        try:
            return resp.json()
        except ValueError:
            raise AdapterError(f"finnhub {path} returned non-JSON") from None

    def supports(self, ticker: str) -> bool:
        return parse_ticker(ticker)[1] == "US"

    def get_quotes(self, tickers: Iterable[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        for ticker in dict.fromkeys(tickers):
            symbol, exch = parse_ticker(ticker)
            if exch != "US":
                continue
            data = self._get("/quote", symbol=symbol)
            price, ts = to_decimal(data.get("c")), data.get("t")
            # Finnhub answers unknown symbols with zeros rather than an error.
            if price is None or price <= 0 or not ts:
                continue
            out[f"{symbol}.US"] = Quote(
                ticker=f"{symbol}.US",
                price=price,
                currency="USD",
                as_of=datetime.fromtimestamp(int(ts), tz=timezone.utc),
                source=self.name,
                prev_close=to_decimal(data.get("pc")),
            )
        return out

    def get_news(self, ticker: str, since: date, until: date) -> list[NewsItem]:
        symbol, _ = parse_ticker(ticker)
        rows = self._get("/company-news", symbol=symbol, **{"from": since.isoformat(), "to": until.isoformat()})
        items = []
        for r in rows or []:
            if not r.get("headline") or not r.get("datetime"):
                continue
            items.append(
                NewsItem(
                    ticker=f"{symbol}.US",
                    published_at=datetime.fromtimestamp(int(r["datetime"]), tz=timezone.utc),
                    headline=r["headline"].strip(),
                    summary=(r.get("summary") or "")[:1000],
                    source=r.get("source") or "",
                    url=r.get("url") or "",
                    provider=self.name,
                    external_id=str(r.get("id") or r.get("url")),
                )
            )
        return items

    def get_earnings(self, ticker: str, since: date, until: date) -> list[CalendarItem]:
        from atlas.prices.eodhd import market_close_utc

        symbol, exch = parse_ticker(ticker)
        if exch != "US":
            return []
        data = self._get("/calendar/earnings", symbol=symbol, **{"from": since.isoformat(), "to": until.isoformat()})
        items = []
        for r in (data or {}).get("earningsCalendar", []):
            if not r.get("date"):
                continue
            hour = {"bmo": "before open", "amc": "after close", "dmh": "during market"}.get(r.get("hour") or "", "")
            items.append(CalendarItem(
                kind="earnings", at=market_close_utc(date.fromisoformat(r["date"])),
                title=f"{symbol}.US earnings" + (f" ({hour})" if hour else ""), provider=self.name,
                external_id=f"earn:{symbol}.US:{r['date']}", ticker=f"{symbol}.US",
                detail={"timing": hour, "eps_estimate": r.get("epsEstimate"), "quarter": r.get("quarter")},
            ))
        return items
