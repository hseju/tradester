"""SnapTradeBroker: the real Wealthsimple connection. NOT IMPLEMENTED (stub).

Planned mapping onto the SnapTrade API (https://docs.snaptrade.com):
  list_accounts   -> GET  /accounts
  list_holdings   -> GET  /accounts/{accountId}/holdings
  list_orders     -> GET  /accounts/{accountId}/orders
  place_order     -> POST /trade/impact + POST /trade/{tradeId}  (two-step)

Per the spec, trading through SnapTrade stays off until we confirm it works
for these accounts and is within Wealthsimple's terms; `can_trade` is False
and place_order refuses even once reads are implemented. Unofficial
Wealthsimple trading libraries are never used.

Credentials (client id, consumer key, per-user secret) belong on the server,
never in the app or the repository.
"""

from __future__ import annotations

from datetime import datetime

from atlas.brokers.base import BrokerAccount, BrokerAdapter, BrokerHolding, BrokerOrder, OrderNotSupported, OrderRequest

_TODO = "SnapTradeBroker is a stub; use ManualBroker (CSV import) for now."


class SnapTradeBroker(BrokerAdapter):
    name = "snaptrade"
    can_trade = False

    def __init__(self, client_id: str | None = None, consumer_key: str | None = None,
                 user_id: str | None = None, user_secret: str | None = None):
        self._configured = all([client_id, consumer_key, user_id, user_secret])

    def list_accounts(self) -> list[BrokerAccount]:
        raise NotImplementedError(_TODO)

    def list_holdings(self, account_id: str | None = None) -> list[BrokerHolding]:
        raise NotImplementedError(_TODO)

    def list_orders(self, account_id: str | None = None, since: datetime | None = None) -> list[BrokerOrder]:
        raise NotImplementedError(_TODO)

    def place_order(self, request: OrderRequest) -> BrokerOrder:
        raise OrderNotSupported("Trading through SnapTrade is disabled until it is confirmed for your accounts.")
