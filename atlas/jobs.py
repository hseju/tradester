"""Scheduled jobs (APScheduler, Toronto time).

  prices        every 5 min in market hours: refresh prices + FX, work open
                paper orders, Risk Officer scan (files a report on any breach)
  news          every 30 min, 07:00-17:30 on weekdays (includes the morning scan)
  daily_close   16:45 on weekdays: final prices, net-worth snapshots,
                the Bookkeeper's daily report

Every job is a plain function of the app, so `atlas jobs run NAME` runs one
by hand and tests call them directly.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from atlas import models
from atlas.app import AtlasApp
from atlas.config import MARKET_TZ
from atlas.market import is_market_open

log = logging.getLogger("atlas.jobs")


def job_prices(app: AtlasApp, force: bool = False) -> dict:
    if not force and not is_market_open(app.now()):
        return {"skipped": "market closed"}
    with app.services() as s:
        res = s.bookkeeper.refresh_prices(s.prices)
        changed = s.paper.process_open_orders()
        reports = s.risk.file_breach_reports(s.desk, models.BOOK_REAL)
        reports += s.risk.file_breach_reports(s.desk, models.BOOK_PAPER)
    out = {"updated": res.updated, "failed": res.failed, "fx": res.fx,
           "orders_changed": [o.id for o in changed], "breach_reports": [r.id for r in reports]}
    log.info("prices: %s", out)
    return out


def job_news(app: AtlasApp, days: int = 2) -> dict:
    with app.services() as s:
        today = s.books.now().date()
        added = {}
        for t in s.books.tracked_tickers():
            items = s.prices.get_news(t, today - timedelta(days=days), today)
            added[t] = s.bookkeeper.record_headlines(items)
    log.info("news: %s", added)
    return {"added": added}


def job_daily_close(app: AtlasApp) -> dict:
    with app.services() as s:
        s.bookkeeper.refresh_prices(s.prices)
        s.paper.process_open_orders()
        real = s.bookkeeper.file_report(s.desk, models.BOOK_REAL)
        paper = s.bookkeeper.snapshot(models.BOOK_PAPER)
        breaches = s.risk.file_breach_reports(s.desk, models.BOOK_REAL)
    out = {"bookkeeper_report": real.id, "paper_snapshot": paper.id, "breach_reports": [r.id for r in breaches]}
    log.info("daily_close: %s", out)
    return out


JOBS = {"prices": job_prices, "news": job_news, "daily_close": job_daily_close}


def build_scheduler(app: AtlasApp, blocking: bool = True):
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = (BlockingScheduler if blocking else BackgroundScheduler)(timezone=MARKET_TZ)
    common = {"max_instances": 1, "coalesce": True, "misfire_grace_time": 120}
    sched.add_job(job_prices, CronTrigger(day_of_week="mon-fri", hour="9-15", minute="*/5", timezone=MARKET_TZ),
                  args=[app], id="prices", **common)
    sched.add_job(job_prices, CronTrigger(day_of_week="mon-fri", hour=16, minute=5, timezone=MARKET_TZ),
                  args=[app], kwargs={"force": True}, id="prices_close", **common)
    sched.add_job(job_news, CronTrigger(day_of_week="mon-fri", hour="7-17", minute="0,30", timezone=MARKET_TZ),
                  args=[app], id="news", **common)
    sched.add_job(job_daily_close, CronTrigger(day_of_week="mon-fri", hour=16, minute=45, timezone=MARKET_TZ),
                  args=[app], id="daily_close", **common)
    return sched
