"""Scheduled jobs (APScheduler, Toronto time).

  prices        every 5 min in market hours: refresh prices + FX, work open
                paper orders, Risk Officer scan (files a report on any breach);
                runs the Advisor at once if a bucket drifts past its band
  market_watch  every 15 min in market hours: news, classification, the
                24-hour-rule flags, "why did this move"
  morning_scan  08:00 on weekdays: news + earnings/economic calendar + BoC rate + USD/CAD
  daily_close   16:45 on weekdays: final prices, net-worth snapshots,
                the Bookkeeper's daily report
  advisor       17:00 on weekdays: drift, goal checks, rebalance plan for your approval

Every job is a plain function of the app, so `atlas jobs run NAME` runs one
by hand and tests call them directly.
"""

from __future__ import annotations

import logging

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
        drifted = [b.bucket for b in s.books.allocation() if b.outside_band]
    out = {"updated": res.updated, "failed": res.failed, "fx": res.fx,
           "orders_changed": [o.id for o in changed], "breach_reports": [r.id for r in reports]}
    if drifted:  # the Advisor runs "when any part of your mix drifts past its band"
        out["advisor"] = job_advisor(app)
    log.info("prices: %s", out)
    return out


def job_market_watch(app: AtlasApp, morning: bool = False) -> dict:
    from atlas.agents import MarketWatch

    with app.services() as s:
        res = MarketWatch(s).run(morning=morning)
        out = {"headlines_added": res.headlines_added, "classified": res.classified, "events_added": res.events_added,
               "used_claude": res.used_llm, "reports": [r.id for r in res.reports]}
    log.info("market_watch: %s", out)
    return out


def job_morning_scan(app: AtlasApp) -> dict:
    return job_market_watch(app, morning=True)


def job_advisor(app: AtlasApp) -> dict:
    from atlas.agents import Advisor

    with app.services() as s:
        reports = Advisor(s).run()
        out = {"reports": [r.id for r in reports]}
    log.info("advisor: %s", out)
    return out


# kept for the `atlas news` command
def job_news(app: AtlasApp, days: int = 2) -> dict:
    return job_market_watch(app)


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


JOBS = {"prices": job_prices, "market_watch": job_market_watch, "morning_scan": job_morning_scan,
        "daily_close": job_daily_close, "advisor": job_advisor}


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
    sched.add_job(job_market_watch, CronTrigger(day_of_week="mon-fri", hour="9-16", minute="*/15", timezone=MARKET_TZ),
                  args=[app], id="market_watch", **common)
    sched.add_job(job_morning_scan, CronTrigger(day_of_week="mon-fri", hour=8, minute=0, timezone=MARKET_TZ),
                  args=[app], id="morning_scan", **common)
    sched.add_job(job_advisor, CronTrigger(day_of_week="mon-fri", hour=17, minute=0, timezone=MARKET_TZ),
                  args=[app], id="advisor", **common)
    sched.add_job(job_daily_close, CronTrigger(day_of_week="mon-fri", hour=16, minute=45, timezone=MARKET_TZ),
                  args=[app], id="daily_close", **common)
    return sched
