"""Wiring: one place that builds the services from settings."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from sqlalchemy.orm import Session

from atlas.bookkeeper import Books, Bookkeeper
from atlas.brokers import PaperBroker
from atlas.config import Settings
from atlas.db import create_all, make_engine, make_session_factory, utcnow
from atlas.prices.service import PriceService
from atlas.reports import ReportDesk
from atlas.risk import RiskOfficer, RuleBook
from atlas.trader import Trader


@dataclass
class Services:
    """Everything bound to one database session (one unit of work)."""

    session: Session
    settings: Settings
    prices: PriceService
    bookkeeper: Bookkeeper
    rules: RuleBook
    risk: RiskOfficer
    desk: ReportDesk
    paper: PaperBroker
    trader: Trader

    @property
    def books(self) -> Books:
        """Read-only view handed to agents. (Same object, typed as Books.)"""
        return self.bookkeeper


class AtlasApp:
    def __init__(self, settings: Settings | None = None, prices: PriceService | None = None, now=utcnow):
        self.settings = settings or Settings.from_env()
        self.engine = make_engine(self.settings.db_url)
        create_all(self.engine)
        self.session_factory = make_session_factory(self.engine)
        self.prices = prices or PriceService.from_settings(self.settings)
        self.now = now

    @contextmanager
    def services(self) -> Iterator[Services]:
        session = self.session_factory()
        try:
            bk = Bookkeeper(session, self.settings, now=self.now)
            rules = RuleBook(session, self.settings)
            risk = RiskOfficer(bk, rules)
            paper = PaperBroker(session, self.prices, bk, self.settings.paper_slippage_bps, now=self.now)
            if self.settings.trading_broker != "paper":
                raise ValueError("phase 1 supports ATLAS_TRADING_BROKER=paper only")
            yield Services(session, self.settings, self.prices, bk, rules, risk, ReportDesk(session), paper,
                           Trader(session, risk, rules, paper))
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
