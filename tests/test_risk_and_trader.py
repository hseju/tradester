from datetime import timedelta
from decimal import Decimal

import pytest

from atlas import models
from atlas.config import Settings
from atlas.risk import RuleBook, RuleChangeRefused, TradeIntent
from atlas.trader import TradeRefused


def buy(acct, ticker, qty, limit):
    return TradeIntent(acct, ticker, "BUY", Decimal(qty), Decimal(limit))


def sell(acct, ticker, qty, limit):
    return TradeIntent(acct, ticker, "SELL", Decimal(qty), Decimal(limit))


def blocked(decision):
    return {c.rule for c in decision.checks if not c.passed}


def test_price_move_breach_is_reported(seeded):
    with seeded.services() as s:
        breaches = s.risk.scan()
        assert [(b.rule, b.subject) for b in breaches] == [("max_single_stock_pct", "NVDA.US")]
        reps = s.risk.file_breach_reports(s.desk)
        assert reps[0].stance.value == "TRIM" and reps[0].wrong_if and reps[0].can_support_trade
        assert s.risk.file_breach_reports(s.desk) == []  # not re-filed at the same level


def test_single_stock_cap_blocks_buys_not_trims(seeded):
    with seeded.services() as s:
        s.bookkeeper.record_deposit("paper-nonreg", "USD", Decimal(5000))
        assert "max_single_stock_pct" in blocked(s.risk.check_trade(buy("paper-nonreg", "NVDA.US", 1, 190)))
        assert s.risk.check_trade(sell("paper-nonreg", "NVDA.US", 15, 189)).passed


def test_etfs_are_exempt_from_stock_and_sector_caps(seeded):
    with seeded.services() as s:
        d = s.risk.check_trade(buy("paper-tfsa", "XEQT.TO", 10, 36))
        assert d.passed
        assert not next(c for c in d.checks if c.rule == "max_single_stock_pct").applicable


def test_sector_cap(seeded):
    with seeded.services() as s:
        s.rules.set("max_single_stock_pct", Decimal(50), actor="user")
        s.rules.set("max_sector_pct", Decimal(20), actor="user")
        s.bookkeeper.record_deposit("paper-nonreg", "USD", Decimal(5000))
        assert blocked(s.risk.check_trade(buy("paper-nonreg", "AAPL.US", 10, 270))) >= {"max_sector_pct"}


def test_cash_floor_and_funds(seeded):
    with seeded.services() as s:
        s.rules.set("min_cash_pct", Decimal(6), actor="risk_officer")  # tightening needs no user
        d = s.risk.check_trade(buy("paper-tfsa", "XEQT.TO", 24, 36))  # cash 6.1% -> 5.2%
        assert d.passed is False and "min_cash_pct" in blocked(d)
        assert "funds" in blocked(s.risk.check_trade(buy("paper-tfsa", "XEQT.TO", 100, 36)))
        assert "funds" in blocked(s.risk.check_trade(sell("paper-tfsa", "XEQT.TO", 601, 36)))


def test_limit_only_and_sanity_band(seeded):
    with seeded.services() as s:
        d = s.risk.check_trade(TradeIntent("paper-tfsa", "XEQT.TO", "BUY", Decimal(1), None, "MARKET"))
        assert "limit_orders_only" in blocked(d)
        assert "max_limit_deviation_pct" in blocked(s.risk.check_trade(buy("paper-tfsa", "XEQT.TO", 1, 360)))


def test_headline_cooldown(seeded, clock):
    with seeded.services() as s:
        s.session.add(models.Headline(ticker="RY.TO", published_at=clock() - timedelta(hours=3),
                                      headline="RBC CEO resigns", provider="t", external_id="1", is_major=True))
        s.session.flush()
        assert "headline_cooldown_hours" in blocked(s.risk.check_trade(sell("paper-nonreg", "RY.TO", 1, 190)))
    clock.t += timedelta(hours=22)
    with seeded.services() as s:
        s.bookkeeper.refresh_prices(s.prices)
        assert s.risk.check_trade(sell("paper-nonreg", "RY.TO", 1, 190)).passed


def test_stale_price_blocks(seeded, clock):
    clock.t += timedelta(hours=1)
    with seeded.services() as s:
        d = s.risk.check_trade(buy("paper-tfsa", "XEQT.TO", 1, 36))
        assert blocked(d) == {"fresh_data"}


def test_daily_loss_limit(seeded, sample):
    with seeded.services() as s:
        p = s.trader.propose(buy("paper-tfsa", "VFV.TO", 5, 160))
        s.trader.approve(p.id)
    sample.prices["VFV.TO"] = Decimal("40")  # -120/share on 5 shares = -600 CAD
    with seeded.services() as s:
        s.bookkeeper.refresh_prices(s.prices)
        assert s.risk.trader_pnl_cad("paper", s.risk._day_start()) == Decimal("-600")
        d = s.risk.check_trade(sell("paper-tfsa", "VFV.TO", 1, 40))
        assert "daily_loss_limit_cad" in blocked(d)


def test_only_user_loosens_rules(seeded):
    with seeded.services() as s:
        rb = s.rules
        with pytest.raises(RuleChangeRefused):
            rb.set("max_single_stock_pct", Decimal(15), actor="atlas")
        with pytest.raises(RuleChangeRefused):
            rb.set("min_cash_pct", Decimal(2), actor="advisor")  # higher-is-stricter rule
        with pytest.raises(RuleChangeRefused):
            rb.set("max_sector_pct", enabled=False, actor="atlas")
        assert rb.set("max_single_stock_pct", Decimal(8), actor="risk_officer").value == 8  # tightening is fine
        assert rb.set("max_single_stock_pct", Decimal(12), actor="user").value == 12
        refused = s.session.query(models.AuditLog).filter_by(action="rule_change_refused").count()
        assert refused == 3


def test_kill_switch(seeded):
    with seeded.services() as s:
        p = s.trader.propose(buy("paper-tfsa", "XEQT.TO", 1, 36))
        s.rules.engage_kill_switch("atlas", "markets halted")  # anyone may engage
        assert "kill_switch" in blocked(s.risk.check_trade(buy("paper-tfsa", "XEQT.TO", 1, 36)))
        assert s.trader.approve(p.id).status == "blocked"
        with pytest.raises(RuleChangeRefused):
            s.rules.release_kill_switch("atlas")
        s.rules.release_kill_switch("user")
        assert s.risk.check_trade(buy("paper-tfsa", "XEQT.TO", 1, 36)).passed


def test_kill_switch_forced_by_env(seeded):
    with seeded.services() as s:
        rb = RuleBook(s.session, Settings(kill_switch_env=True))
        assert rb.kill_switch().engaged and rb.kill_switch().forced_by_env
        with pytest.raises(RuleChangeRefused):
            rb.release_kill_switch("user")


def test_kill_switch_cancels_open_orders(seeded):
    with seeded.services() as s:
        p = s.trader.propose(buy("paper-tfsa", "XEQT.TO", 1, Decimal("35.50")))  # below market: rests
        s.trader.approve(p.id)
        order = s.session.get(models.Order, s.session.get(models.Proposal, p.id).order_id)
        assert order.status == "open"
        s.rules.engage_kill_switch("user", "test")
        assert order.status == "cancelled"


def test_trader_flow_and_paper_fill(seeded):
    with seeded.services() as s:
        p = s.trader.propose(sell("paper-nonreg", "NVDA.US", 15, 189), rationale="trim to cap")
        assert p.status == "proposed"
        with pytest.raises(TradeRefused):
            s.trader.approve(p.id, actor="atlas")
        p = s.trader.approve(p.id)
        assert p.status == "executed"
        o = s.session.get(models.Order, p.order_id)
        # market 190 less 5bp slippage = 189.905, not below the 189 limit
        assert o.status == "filled" and o.fill_price == Decimal("189.9050")
        assert o.realized_pnl == (Decimal("189.9050") - 98) * 15
        assert s.books.portfolio("paper").ticker_weight("NVDA.US") < 10
        assert s.risk.scan("paper") == []


def test_changed_after_approval_is_refused(seeded):
    with seeded.services() as s:
        p = s.trader.propose(sell("paper-nonreg", "NVDA.US", 15, 189))
        p = s.trader.approve(p.id, execute=False)
        p.quantity = Decimal(45)
        with pytest.raises(TradeRefused, match="changed after approval"):
            s.trader.execute(p.id)
        assert p.status == "failed"


def test_agent_proposals_need_sourced_reports(seeded, clock):
    with seeded.services() as s:
        with pytest.raises(TradeRefused, match="at least one report"):
            s.trader.propose(sell("paper-nonreg", "NVDA.US", 15, 189), proposed_by="advisor")
        weak = s.desk.submit(dict(agent="advisor", as_of=clock(), subject="NVDA.US", finding="Trim it.",
                                  stance="TRIM", confidence="0.5", urgency="low",
                                  evidence=[{"claim": "gut feel"}], wrong_if="x", next_check=clock()))
        with pytest.raises(TradeRefused, match="no sourced claims"):
            s.trader.propose(sell("paper-nonreg", "NVDA.US", 15, 189), proposed_by="advisor", report_ids=[weak.id])
        good = s.risk.file_breach_reports(s.desk)[0]
        p = s.trader.propose(sell("paper-nonreg", "NVDA.US", 15, 189), proposed_by="advisor", report_ids=[good.id])
        assert p.status == "proposed"


def test_paper_order_rests_then_fills_or_expires(seeded, sample, clock):
    with seeded.services() as s:
        p = s.trader.approve(s.trader.propose(buy("paper-tfsa", "XEQT.TO", 2, Decimal("35.50"))).id)
        oid = p.order_id
    sample.prices["XEQT.TO"] = Decimal("35.40")
    with seeded.services() as s:
        s.bookkeeper.refresh_prices(s.prices)
        s.paper.process_open_orders()
        o = s.session.get(models.Order, oid)
        assert o.status == "filled" and o.fill_price <= Decimal("35.50")
        p2 = s.trader.approve(s.trader.propose(buy("paper-tfsa", "XEQT.TO", 2, Decimal("34.00"))).id)
    clock.t += timedelta(hours=7)  # past the 16:00 ET close
    with seeded.services() as s:
        s.paper.process_open_orders()
        assert s.session.get(models.Order, p2.order_id).status == "expired"
