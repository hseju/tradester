"""Bank of Canada Valet API: official daily CAD exchange rates (free, no key).

Docs: https://www.bankofcanada.ca/valet/docs . Rates publish once per business
day around 16:30 ET.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from decimal import Decimal

import httpx2 as httpx

from atlas.market import ET
from atlas.prices.base import AdapterError, FxQuote, to_decimal

POLICY_RATE_SERIES = "V39079"  # target for the overnight rate

BASE_URL = "https://www.bankofcanada.ca/valet"


class BankOfCanadaFx:
    name = "bankofcanada"

    def __init__(self, client: httpx.Client | None = None, timeout: float = 15.0):
        self._client = client or httpx.Client(timeout=timeout)

    def get_rate(self, pair: str) -> FxQuote:
        pair = pair.upper()
        if len(pair) != 6 or not pair.endswith("CAD"):
            raise AdapterError(f"Bank of Canada publishes XXXCAD pairs only, not {pair}")
        series = f"FX{pair}"
        try:
            resp = self._client.get(f"{BASE_URL}/observations/{series}/json", params={"recent": 1})
        except httpx.HTTPError as exc:
            raise AdapterError(f"bank of canada request failed: {exc}") from None
        if resp.status_code != 200:
            raise AdapterError(f"bank of canada {series} -> HTTP {resp.status_code}")
        obs = (resp.json().get("observations") or [])
        if not obs:
            raise AdapterError(f"bank of canada returned no observations for {series}")
        last = obs[-1]
        rate = to_decimal((last.get(series) or {}).get("v"))
        if rate is None:
            raise AdapterError(f"bank of canada observation for {series} has no value")
        day = date.fromisoformat(last["d"])
        as_of = datetime.combine(day, time(16, 30), tzinfo=ET).astimezone(timezone.utc)
        return FxQuote(pair=pair, rate=rate, as_of=as_of, source=self.name)

    def get_policy_rate(self) -> tuple[Decimal, date]:
        """The Bank of Canada's target for the overnight rate, in percent, and its effective date."""
        try:
            resp = self._client.get(f"{BASE_URL}/observations/{POLICY_RATE_SERIES}/json", params={"recent": 1})
        except httpx.HTTPError as exc:
            raise AdapterError(f"bank of canada request failed: {exc}") from None
        if resp.status_code != 200:
            raise AdapterError(f"bank of canada policy rate -> HTTP {resp.status_code}")
        obs = resp.json().get("observations") or []
        rate = to_decimal((obs[-1].get(POLICY_RATE_SERIES) or {}).get("v")) if obs else None
        if rate is None:
            raise AdapterError("bank of canada returned no policy rate")
        return rate, date.fromisoformat(obs[-1]["d"])
