from datetime import timedelta
from decimal import Decimal

import pytest

from atlas import models
from atlas.brokers import ManualBroker, OrderNotSupported, OrderRequest
from atlas.brokers.base import BrokerError


def test_sample_book_values_exactly(seeded):
    with seeded.services() as s:
        v = s.books.portfolio()
        # 600*36 + 60*160 + 900 | 12*620*1.4 + 500*14 + 150*42 + 600 + 300*1.4 | 120*44 + 2500 | 45*190*1.4 + 18*270*1.4 + 20*190 + 1000
        assert {a.id: a.total_cad for a in v.accounts} == {
            "tfsa": Decimal("32100"), "rrsp": Decimal("24736"), "fhsa": Decimal("7780"), "nonreg": Decimal("23574")}
        assert v.total_cad == Decimal("88190")
        assert v.cash_cad == Decimal("5420")
        assert round(v.ticker_weight("NVDA.US"), 2) == Decimal("13.57")
        assert v.uses_sample_data
        assert [f.code for f in v.flags] == ["sample_data"]
        # Day change only comes from prev_close; sample NVDA "ran up" 2.5%
        assert v.day_change_cad > 0


def test_allocation_look_through_and_drift(seeded):
    with seeded.services() as s:
        alloc = {b.bucket: b for b in s.books.allocation()}
        # XEQT 21,600 splits 25/45/30
        assert alloc["ca_equity"].value_cad == Decimal("21600") * Decimal("0.25") + 5280 + 3800
        assert alloc["us_equity"].outside_band and alloc["us_equity"].drift_pct > 5
        assert not alloc["intl_equity"].outside_band
        assert sum(b.pct for b in alloc.values()) == pytest.approx(Decimal(100))


def test_goals_progress(seeded):
    with seeded.services() as s:
        house = next(g for g in s.books.goals() if g.id == "house")
        assert house.current_cad == Decimal("7780")
        assert house.months_left == 31  # Nov 2026 -> Jun 2029
        assert house.needed_per_month_no_growth == (Decimal(40000) - 7780) / 31


def test_flags_missing_and_stale_prices_and_fx(seeded, clock):
    with seeded.services() as s:
        s.session.execute(models.Price.__table__.delete().where(models.Price.ticker == "RY.TO"))
        s.session.execute(models.FxRate.__table__.delete())
    clock.t += timedelta(hours=2)  # still market hours: every price is now 2h old
    with seeded.services() as s:
        v = s.books.portfolio()
        codes = {(f.code, f.subject) for f in v.flags}
        assert ("missing_price", "RY.TO") in codes
        assert ("missing_fx", "USD") in codes
        assert ("stale_price", "XEQT.TO") in codes
        # USD holdings and RY are left out of the total, not guessed
        assert v.total_cad == Decimal("32100") + 7000 + 6300 + 600 + 7780 + 1000


def test_numbers_dont_add_up_flags(seeded):
    with seeded.services() as s:
        s.session.get(models.CashBalance, ("tfsa", "CAD")).amount = Decimal("-5")
        v = s.books.portfolio()
        assert any(f.code == "integrity" and "Negative CAD cash" in f.message for f in v.flags)


def test_bookkeeper_report_is_facts_only(seeded):
    with seeded.services() as s:
        r = s.bookkeeper.file_report(s.desk)
        assert r.stance.value == "NO VIEW" and r.agent == "bookkeeper"
        assert "SAMPLE" in r.finding
        assert r.evidence[0].source.startswith("record:net_worth_snapshots/")


def test_manual_broker_native_csv(app):
    text = ("account_id,account_name,account_type,symbol,exchange,quantity,avg_cost,currency\n"
            "t1,My TFSA,TFSA,XEQT,TSX,10,30,CAD\n"
            "t1,My TFSA,TFSA,XEQT,TSX,10,32,CAD\n"
            "t1,My TFSA,TFSA,CASH,,250.50,,CAD\n"
            "n1,Margin,Margin,SHOP,TSX,5,,CAD\n"
            "n1,Margin,Margin,FOO,MOON,5,1,CAD\n")
    broker = ManualBroker(text=text)
    accts = {a.id: a for a in broker.list_accounts()}
    assert accts["t1"].account_type == "TFSA" and accts["t1"].cash == {"CAD": Decimal("250.50")}
    assert accts["n1"].account_type == "NON_REGISTERED"
    assert any("unknown exchange" in w for w in broker.warnings)
    assert any("no cost for SHOP" in w for w in broker.warnings)
    with app.services() as s:
        res = s.bookkeeper.import_from_broker(broker)
        pos = s.session.query(models.Position).filter_by(account_id="t1").one()
        assert pos.quantity == 20 and pos.avg_cost == 31  # duplicate rows merged at weighted cost
        assert "SHOP.TO" in res.new_instruments
        assert not s.books.instrument("SHOP.TO").classified
    with pytest.raises(OrderNotSupported):
        broker.place_order(OrderRequest("t1", "XEQT.TO", "BUY", Decimal(1), Decimal(30)))


def test_manual_broker_export_style_headers_and_reconcile(app):
    text = ("Account Name,Account Type,Symbol,Exchange,Name,Quantity,Market Price,Market Price Currency,"
            "Book Value (Market),Market Value\n"
            "Main,RRSP,VFV,TSX,Vanguard S&P 500,10,160.00,CAD,1200.00,1600.00\n"
            "Main,RRSP,AAPL,NASDAQ,Apple,2,270.00,USD,364.00,999.00\n")
    broker = ManualBroker(text=text)
    h = {x.symbol: x for x in broker.list_holdings()}
    assert h["VFV"].avg_cost == Decimal("120") and h["AAPL"].currency == "USD" and h["AAPL"].exchange == "US"
    with app.services() as s:
        res = s.bookkeeper.import_from_broker(broker)
        assert [f.subject for f in res.flags if f.code == "reconcile"] == ["AAPL.US"]


def test_manual_broker_rejects_bad_csv():
    with pytest.raises(BrokerError):
        ManualBroker(text="foo,bar\n1,2\n")


def test_apply_fill_guards(seeded):
    from atlas.brokers import InsufficientFunds

    with seeded.services() as s:
        with pytest.raises(InsufficientFunds):
            s.bookkeeper.apply_fill("paper-tfsa", "XEQT.TO", "BUY", Decimal(1000), Decimal(36))
        with pytest.raises(InsufficientFunds):
            s.bookkeeper.apply_fill("paper-tfsa", "XEQT.TO", "SELL", Decimal(601), Decimal(36))
        realized = s.bookkeeper.apply_fill("paper-tfsa", "XEQT.TO", "SELL", Decimal(100), Decimal(36))
        assert realized == (Decimal(36) - Decimal("30.10")) * 100
        assert s.session.get(models.CashBalance, ("paper-tfsa", "CAD")).amount == Decimal(900 + 3600)
        # the real book is untouched by paper fills
        assert s.books.portfolio().total_cad == Decimal("88190")


def test_clone_to_paper_resets(seeded):
    with seeded.services() as s:
        s.bookkeeper.apply_fill("paper-tfsa", "XEQT.TO", "SELL", Decimal(100), Decimal(36))
        s.bookkeeper.clone_to_paper()
        assert s.books.portfolio("paper").position_qty("paper-tfsa", "XEQT.TO") == 600
