"""ManualBroker: reads holdings from a spreadsheet you export or type in.

Native CSV columns (header names are case-insensitive):

    account_id, account_name, account_type, account_currency,
    symbol, exchange, quantity, avg_cost, currency,
    name, asset_type, market_price, market_value

Cash is one row per account and currency with symbol CASH and the amount in
`quantity`. Common broker-export headers ("Account Type", "Symbol",
"Book Value (Market)", "Market Value", ...) are also recognised, so a
holdings export can often be imported as-is; check the import flags after.

The ManualBroker can't place orders: you trade in your broker's app, then
re-import.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from atlas.brokers.base import (
    ACCOUNT_TYPES,
    BrokerAccount,
    BrokerAdapter,
    BrokerError,
    BrokerHolding,
    BrokerOrder,
    OrderNotSupported,
    OrderRequest,
)
from atlas.market import currency_for, normalize_exchange
from atlas.prices.base import to_decimal

# canonical field -> accepted headers, in priority order
ALIASES: dict[str, list[str]] = {
    "account_id": ["account_id", "account number", "account_number", "account no", "account"],
    "account_name": ["account_name", "account name", "account nickname"],
    "account_type": ["account_type", "account type", "account classification"],
    "account_currency": ["account_currency", "account currency"],
    "symbol": ["symbol", "ticker", "security symbol"],
    "exchange": ["exchange", "market", "listing exchange"],
    "quantity": ["quantity", "qty", "shares", "units", "amount"],
    "avg_cost": ["avg_cost", "average cost", "avg cost", "average price", "acb per share"],
    # Total cost; prefer the one in the security's own currency.
    "book_value": ["book_value", "book value (market)", "book value", "total cost", "book value (cad)"],
    "currency": ["currency", "market price currency", "market value currency", "book value currency (market)"],
    "name": ["name", "description", "security name", "security"],
    "asset_type": ["asset_type", "security type", "type"],
    "market_price": ["market_price", "market price", "price", "last price"],
    "market_value": ["market_value", "market value"],
}

ACCOUNT_TYPE_ALIASES = {
    "TFSA": "TFSA", "TAX-FREE SAVINGS ACCOUNT": "TFSA",
    "RRSP": "RRSP", "RSP": "RRSP", "SPOUSAL RRSP": "RRSP",
    "FHSA": "FHSA", "FIRST HOME SAVINGS ACCOUNT": "FHSA",
    "RESP": "RESP", "LIRA": "LIRA", "RRIF": "RRIF",
    "NON_REGISTERED": "NON_REGISTERED", "NON-REGISTERED": "NON_REGISTERED", "NON REGISTERED": "NON_REGISTERED",
    "PERSONAL": "NON_REGISTERED", "TAXABLE": "NON_REGISTERED", "MARGIN": "NON_REGISTERED",
    "JOINT": "NON_REGISTERED", "INDIVIDUAL": "NON_REGISTERED",
    "CASH": "CASH", "CHEQUING": "CASH", "SAVINGS": "CASH",
}

CASH_SYMBOLS = {"CASH", "CAD", "USD", "$CASH"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "account"


def normalize_account_type(raw: str) -> str:
    key = (raw or "").strip().upper()
    t = ACCOUNT_TYPE_ALIASES.get(key)
    if t:
        return t
    for alias, canon in ACCOUNT_TYPE_ALIASES.items():
        if alias in key:
            return canon
    return "OTHER"


class ManualBroker(BrokerAdapter):
    name = "manual"
    can_trade = False

    def __init__(self, source: str | Path | None = None, *, text: str | None = None):
        if (source is None) == (text is None):
            raise ValueError("give a CSV path or CSV text")
        self._text = Path(source).read_text(encoding="utf-8-sig") if source is not None else text
        self.warnings: list[str] = []
        self._accounts, self._holdings = self._parse()

    # -- parsing ------------------------------------------------------------
    def _parse(self) -> tuple[dict[str, BrokerAccount], list[BrokerHolding]]:
        reader = csv.DictReader(io.StringIO(self._text))
        if not reader.fieldnames:
            raise BrokerError("CSV has no header row")
        header = {h.strip().lower(): h for h in reader.fieldnames if h}
        col = {}
        for canon, names in ALIASES.items():
            for n in names:
                if n in header:
                    col[canon] = header[n]
                    break
        for required in ("symbol", "quantity"):
            if required not in col:
                raise BrokerError(f"CSV is missing a '{required}' column")
        if "account_id" not in col and "account_name" not in col and "account_type" not in col:
            raise BrokerError("CSV needs an account_id, account_name or account_type column")

        accounts: dict[str, dict] = {}
        holdings: list[BrokerHolding] = []
        for lineno, row in enumerate(reader, start=2):
            get = lambda f: (row.get(col[f]) or "").strip() if f in col else ""  # noqa: E731
            symbol = get("symbol").upper()
            if not symbol:
                continue
            acct_type = normalize_account_type(get("account_type"))
            acct_name = get("account_name") or acct_type.replace("_", "-").title()
            acct_id = _slug(get("account_id") or acct_name)
            acct = accounts.setdefault(
                acct_id,
                {
                    "id": acct_id,
                    "name": acct_name,
                    "account_type": acct_type if acct_type in ACCOUNT_TYPES else "OTHER",
                    "base_currency": (get("account_currency") or "CAD").upper(),
                    "cash": {},
                    "external_id": get("account_id") or None,
                },
            )
            qty = to_decimal(get("quantity").replace(",", ""))
            if qty is None:
                self.warnings.append(f"line {lineno}: unreadable quantity {get('quantity')!r}; row skipped")
                continue
            if symbol in CASH_SYMBOLS:
                cur = (get("currency") or (symbol if symbol in {"CAD", "USD"} else acct["base_currency"])).upper()
                acct["cash"][cur] = acct["cash"].get(cur, Decimal(0)) + qty
                continue
            try:
                exchange = normalize_exchange(get("exchange") or "TO")
            except ValueError:
                self.warnings.append(f"line {lineno}: unknown exchange {get('exchange')!r} for {symbol}; row skipped")
                continue
            currency = (get("currency") or currency_for(exchange)).upper()
            avg_cost = to_decimal(get("avg_cost").replace(",", "").replace("$", ""))
            if avg_cost is None:
                book = to_decimal(get("book_value").replace(",", "").replace("$", ""))
                if book is not None and qty:
                    avg_cost = book / qty
            if avg_cost is None:
                self.warnings.append(f"line {lineno}: no cost for {symbol}; recorded at 0 (gains will be overstated)")
                avg_cost = Decimal(0)
            holdings.append(
                BrokerHolding(
                    account_id=acct_id,
                    symbol=symbol,
                    exchange=exchange,
                    quantity=qty,
                    avg_cost=avg_cost,
                    currency=currency,
                    name=get("name"),
                    asset_type=(get("asset_type").lower() or None),
                    market_price=to_decimal(get("market_price").replace(",", "").replace("$", "")),
                    market_value=to_decimal(get("market_value").replace(",", "").replace("$", "")),
                )
            )
        return {k: BrokerAccount(**v) for k, v in accounts.items()}, holdings

    # -- broker interface -----------------------------------------------------
    def list_accounts(self) -> list[BrokerAccount]:
        return list(self._accounts.values())

    def list_holdings(self, account_id: str | None = None) -> list[BrokerHolding]:
        return [h for h in self._holdings if account_id is None or h.account_id == account_id]

    def list_orders(self, account_id: str | None = None, since: datetime | None = None) -> list[BrokerOrder]:
        return []  # a spreadsheet has no order history

    def place_order(self, request: OrderRequest) -> BrokerOrder:
        raise OrderNotSupported(
            "The manual broker can't place orders. Place it yourself in your broker's app, then re-import."
        )
