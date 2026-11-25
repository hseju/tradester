import json
from datetime import date
from decimal import Decimal

import httpx2 as httpx
import pytest

from atlas.config import Settings
from atlas.prices.bankofcanada import BankOfCanadaFx
from atlas.prices.base import AdapterError
from atlas.prices.eodhd import EODHDAdapter
from atlas.prices.finnhub import FinnhubAdapter
from atlas.prices.sample import SampleAdapter
from atlas.prices.service import PriceService


def client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_eodhd_batches_and_parses_quotes():
    seen = []

    def handler(req: httpx.Request):
        seen.append(req.url)
        assert req.url.params["api_token"] == "k" and req.url.params["fmt"] == "json"
        assert req.url.path == "/api/real-time/XEQT.TO"
        assert req.url.params["s"] == "VFV.TO,ZAG.TO"
        return httpx.Response(200, json=[
            {"code": "XEQT.TO", "timestamp": 1764000000, "close": 36.12, "previousClose": 35.9},
            {"code": "VFV.TO", "timestamp": 1764000000, "close": "NA", "previousClose": 159},
            {"code": "ZAG.TO", "timestamp": "NA", "close": 14.01},
        ])

    quotes = EODHDAdapter("k", client=client(handler)).get_quotes(["XEQT.TO", "VFV.TO", "ZAG.TO"])
    assert list(quotes) == ["XEQT.TO"]  # "NA" rows are dropped, not turned into zeros
    q = quotes["XEQT.TO"]
    assert q.price == Decimal("36.12") and q.prev_close == Decimal("35.9") and q.currency == "CAD"
    assert q.as_of.tzinfo is not None and len(seen) == 1


def test_eodhd_single_quote_eod_and_news():
    def handler(req):
        if req.url.path.startswith("/api/real-time/"):
            return httpx.Response(200, json={"code": "AAPL.US", "timestamp": 1764000000, "close": 271.5})
        if req.url.path == "/api/eod/XIC.TO":
            return httpx.Response(200, json=[{"date": "2026-11-20", "open": 43, "high": 44, "low": 42.5,
                                              "close": 43.8, "adjusted_close": 43.8, "volume": 1000}])
        if req.url.path == "/api/news":
            return httpx.Response(200, json=[{"date": "2026-11-20T14:00:00+00:00", "title": "Bank earnings beat",
                                              "content": "...", "link": "https://x/1"}])
        return httpx.Response(404)

    a = EODHDAdapter("k", client=client(handler))
    assert a.get_quotes(["AAPL.US"])["AAPL.US"].currency == "USD"
    assert a.get_eod("XIC.TO", date(2026, 11, 1), date(2026, 11, 20))[0].close == Decimal("43.8")
    news = a.get_news("RY.TO", date(2026, 11, 19), date(2026, 11, 20))
    assert news[0].headline == "Bank earnings beat" and news[0].external_id == "https://x/1"


def test_eodhd_errors_never_leak_the_key():
    a = EODHDAdapter("SECRETKEY", client=client(lambda r: httpx.Response(401, text=f"bad {r.url}")))
    with pytest.raises(AdapterError) as exc:
        a.get_quotes(["XEQT.TO"])
    assert "SECRETKEY" not in str(exc.value) and "401" in str(exc.value)


def test_finnhub_quotes_us_only_and_ignores_zero_answers():
    def handler(req):
        assert req.headers["X-Finnhub-Token"] == "fk"
        assert "token" not in req.url.params
        sym = req.url.params["symbol"]
        if sym == "NVDA":
            return httpx.Response(200, json={"c": 190.5, "pc": 186.0, "t": 1764000000})
        return httpx.Response(200, json={"c": 0, "pc": 0, "t": 0})

    a = FinnhubAdapter("fk", client=client(handler))
    assert not a.supports("XEQT.TO") and a.supports("NVDA.US")
    quotes = a.get_quotes(["NVDA.US", "ZZZZ.US", "XEQT.TO"])
    assert list(quotes) == ["NVDA.US"] and quotes["NVDA.US"].prev_close == Decimal("186.0")


def test_finnhub_news_and_rate_limit():
    def handler(req):
        if req.url.params.get("symbol") == "AAPL":
            return httpx.Response(200, json=[{"datetime": 1764000000, "headline": "Apple faces antitrust probe",
                                              "source": "Reuters", "url": "https://n/1", "id": 99, "summary": "s"}])
        return httpx.Response(429)

    a = FinnhubAdapter("fk", client=client(handler))
    items = a.get_news("AAPL.US", date(2026, 11, 20), date(2026, 11, 24))
    assert items[0].ticker == "AAPL.US" and items[0].external_id == "99"
    with pytest.raises(AdapterError, match="rate limit"):
        a.get_news("NVDA.US", date(2026, 11, 20), date(2026, 11, 24))


def test_bank_of_canada_fx():
    body = {"observations": [{"d": "2026-11-23", "FXUSDCAD": {"v": "1.3987"}}]}

    def handler(req):
        assert req.url.path == "/valet/observations/FXUSDCAD/json" and req.url.params["recent"] == "1"
        return httpx.Response(200, content=json.dumps(body))

    q = BankOfCanadaFx(client=client(handler)).get_rate("USDCAD")
    assert q.rate == Decimal("1.3987") and q.as_of.isoformat().startswith("2026-11-23T21:30")
    with pytest.raises(AdapterError):
        BankOfCanadaFx(client=client(handler)).get_rate("CADUSD")


def test_service_routing_and_fallback_without_sample_prices():
    def handler(req):
        host = req.url.host
        if host == "finnhub.io":
            return httpx.Response(500)  # Finnhub down -> EODHD covers US names
        if host == "eodhd.com" and req.url.path.endswith(".FOREX"):
            return httpx.Response(200, json={"code": "USDCAD.FOREX", "timestamp": "NA", "close": "NA"})
        if host == "eodhd.com" and "/real-time/" in req.url.path:
            codes = [req.url.path.rsplit("/", 1)[1]] + [c for c in req.url.params.get("s", "").split(",") if c]
            return httpx.Response(200, json=[{"code": c, "timestamp": 1764000000, "close": 10} for c in codes
                                             if c != "XEF.TO"])
        if host == "www.bankofcanada.ca":
            return httpx.Response(503)
        return httpx.Response(404)

    svc = PriceService.from_settings(Settings(eodhd_api_key="e", finnhub_api_key="f"), client=client(handler))
    assert not svc.is_sample
    batch = svc.get_quotes(["NVDA.US", "XEQT.TO", "XEF.TO"])
    assert set(batch.quotes) == {"NVDA.US", "XEQT.TO"} and batch.quotes["NVDA.US"].source == "eodhd"
    assert "XEF.TO" in batch.errors  # real sources configured: no silent sample price
    with pytest.raises(AdapterError):  # BoC down and EODHD forex 404 -> error, not a made-up rate
        svc.get_fx("USDCAD")


def test_service_uses_sample_without_keys():
    svc = PriceService.from_settings(Settings())
    assert svc.is_sample
    assert svc.get_quotes(["XEQT.TO"]).quotes["XEQT.TO"].source == "sample"
    assert isinstance(svc.quote_adapters[0], SampleAdapter)
