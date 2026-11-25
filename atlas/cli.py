"""`atlas` command line. Everything typed here acts as you ("user")."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from atlas import models
from atlas.app import AtlasApp
from atlas.config import Settings

USER = "user"


def _dec(text: str) -> Decimal:
    try:
        return Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not a number: {text}") from None


def _app() -> AtlasApp:
    return AtlasApp(Settings.from_env())


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# -- commands ---------------------------------------------------------------
def cmd_init(a) -> int:
    from atlas.seed import seed_sample

    app = _app()
    with app.services() as s:
        if s.bookkeeper.accounts() and not a.force:
            print("The books already have accounts. Use --force to reload the sample book on top.")
            return 1
        out = seed_sample(s)
    print(f"Loaded sample book: {out['accounts']} accounts, {out['positions']} positions, "
          f"{out['prices_updated']} prices ({'SAMPLE' if app.prices.is_sample else 'live'}).")
    if out["price_failures"]:
        print("Price problems:", out["price_failures"])
    print("Paper book created:", ", ".join(out["paper_accounts"]))
    return 0


def cmd_import(a) -> int:
    from atlas.brokers import ManualBroker

    app = _app()
    with app.services() as s:
        s.rules.ensure_defaults()
        broker = ManualBroker(Path(a.csv))
        res = s.bookkeeper.import_from_broker(broker, replace=not a.merge)
        refresh = s.bookkeeper.refresh_prices(s.prices)
    print(f"Imported {res.accounts} accounts, {res.positions} positions, {res.cash_lines} cash lines.")
    if res.new_instruments:
        print("New instruments (mix/sector guessed - add them to a catalogue CSV with `atlas catalog`):",
              ", ".join(res.new_instruments))
    for f in res.flags:
        print(f"[{f.severity}] {f.message}")
    if refresh.failed:
        print("Price problems:", refresh.failed)
    return 0


def cmd_catalog(a) -> int:
    app = _app()
    with app.services() as s:
        n = s.bookkeeper.load_catalog(Path(a.csv))
    print(f"Loaded {n} instruments.")
    return 0


def cmd_brief(a) -> int:
    from atlas.chat import ToolBox, briefing

    with _app().services() as s:
        print(briefing(ToolBox(s)))
    return 0


def cmd_holdings(a) -> int:
    from atlas.chat.atlas import _fmt_holdings
    from atlas.chat.tools import ToolBox

    with _app().services() as s:
        print(_fmt_holdings(json.loads(ToolBox(s).run("get_holdings", {"book": a.book}))))
    return 0


def cmd_allocation(a) -> int:
    from atlas.chat.atlas import _fmt_allocation
    from atlas.chat.tools import ToolBox

    with _app().services() as s:
        print(_fmt_allocation(json.loads(ToolBox(s).run("get_allocation", {"book": a.book}))))
    return 0


def cmd_chat(a) -> int:
    from atlas.chat import make_atlas

    app = _app()
    atlas = make_atlas(app, a.engine)
    if a.question:
        print(atlas.ask(" ".join(a.question)))
        return 0
    print(f"Atlas ({atlas.name} mode). Ask about your money; 'quit' to leave.")
    while True:
        try:
            q = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if q.lower() in {"quit", "exit", "q"}:
            return 0
        if q:
            print("atlas>", atlas.ask(q), "\n")


def cmd_prices(a) -> int:
    from atlas.jobs import job_prices

    _print_json(job_prices(_app(), force=True))
    return 0


def cmd_news(a) -> int:
    from atlas.jobs import job_news

    _print_json(job_news(_app(), days=a.days))
    return 0


def cmd_rules(a) -> int:
    from atlas.risk import RuleChangeRefused

    with _app().services() as s:
        s.rules.ensure_defaults()
        try:
            if a.rules_cmd == "set":
                s.rules.set(a.key, a.value, actor=USER)
            elif a.rules_cmd == "enable":
                s.rules.set(a.key, enabled=True, actor=USER)
            elif a.rules_cmd == "disable":
                s.rules.set(a.key, enabled=False, actor=USER)
        except (RuleChangeRefused, KeyError, ValueError) as exc:
            print(exc)
            return 1
        ks = s.rules.kill_switch()
        print(f"Kill switch: {'ON - ' + ks.reason if ks.engaged else 'off'}")
        for r in s.rules.all():
            print(f"  {r.key:<26} {str(r.value):>8} {r.unit:<4} {'on ' if r.enabled else 'OFF'}  {r.label}")
    return 0


def cmd_kill(a) -> int:
    from atlas.risk import RuleChangeRefused

    with _app().services() as s:
        try:
            ks = (s.rules.engage_kill_switch(USER, a.reason or "engaged from the CLI") if a.state == "on"
                  else s.rules.release_kill_switch(USER, a.reason or ""))
        except RuleChangeRefused as exc:
            print(exc)
            return 1
    print(f"Kill switch {'ON' if ks.engaged else 'off'}.")
    return 0


def cmd_propose(a) -> int:
    from atlas.market import normalize_ticker
    from atlas.risk import TradeIntent
    from atlas.trader import TradeRefused

    with _app().services() as s:
        intent = TradeIntent(a.account, normalize_ticker(a.ticker), a.side.upper(), a.quantity, a.limit)
        try:
            p = s.trader.propose(intent, rationale=a.why or "", report_ids=a.report or [])
        except TradeRefused as exc:
            print(f"Refused: {exc}")
            return 1
        _print_risk(p)
        if p.status == "proposed":
            print(f"Proposal {p.id} is waiting for you: `atlas approve {p.id}` or `atlas dismiss {p.id}`.")
    return 0 if p.status == "proposed" else 2


def _print_risk(p: models.Proposal) -> None:
    print(f"Proposal {p.id}: {p.side} {p.quantity} {p.ticker} @ {p.limit_price} in {p.account_id} -> {p.status.upper()}")
    for c in p.risk_result.get("checks", []):
        mark = "ok " if c["passed"] else "BLOCK"
        print(f"  [{mark}] {c['label']}: {c['detail']}")


def cmd_approve(a) -> int:
    from atlas.trader import TradeRefused

    with _app().services() as s:
        try:
            p = s.trader.approve(a.id, actor=USER)
        except TradeRefused as exc:
            print(f"Refused: {exc}")
            return 1
        _print_risk(p)
        if p.order_id:
            o = s.session.get(models.Order, p.order_id)
            print(f"Paper order {o.id}: {o.status}" + (f" at {o.fill_price}" if o.fill_price else f" ({o.note})"))
    return 0


def cmd_dismiss(a) -> int:
    from atlas.trader import TradeRefused

    with _app().services() as s:
        try:
            s.trader.dismiss(a.id, actor=USER, reason=a.reason or "")
        except TradeRefused as exc:
            print(f"Refused: {exc}")
            return 1
    print(f"Proposal {a.id} dismissed.")
    return 0


def cmd_orders(a) -> int:
    from atlas.chat.atlas import _fmt_orders
    from atlas.chat.tools import ToolBox

    with _app().services() as s:
        print(_fmt_orders(json.loads(ToolBox(s).run("get_orders", {"limit": a.limit}))))
    return 0


def cmd_reports(a) -> int:
    with _app().services() as s:
        reps = s.desk.recent(agent=a.agent, subject=a.subject, limit=a.limit)
        if a.json:
            _print_json([r.to_public() for r in reps])
        for r in [] if a.json else reps:
            print(f"#{r.id} {r.as_of:%Y-%m-%d %H:%M} {r.agent} on {r.subject} [{r.stance.value}, "
                  f"conf {r.confidence}, {r.urgency.value}]\n    {r.finding}")
    return 0


def cmd_schema(a) -> int:
    from atlas.reports import AgentReport

    _print_json(AgentReport.json_schema())
    return 0


def cmd_paper(a) -> int:
    with _app().services() as s:
        made = s.bookkeeper.clone_to_paper(actor=USER)
    print("Paper book reset from the real book:", ", ".join(made))
    return 0


def cmd_jobs(a) -> int:
    from atlas.jobs import JOBS, build_scheduler

    app = _app()
    if a.jobs_cmd == "run":
        fn = JOBS[a.name]
        _print_json(fn(app, force=True) if a.name == "prices" else fn(app))
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    sched = build_scheduler(app)
    print("Scheduler running (Ctrl-C to stop). Jobs:", ", ".join(j.id for j in sched.get_jobs()))
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


def cmd_serve(a) -> int:
    import uvicorn

    from atlas.api import create_api

    uvicorn.run(create_api(_app()), host=a.host, port=a.port)
    return 0


# -- parser -------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="atlas", description="Atlas personal finance board (phase 1)")
    sub = p.add_subparsers(dest="cmd", required=True)

    x = sub.add_parser("init", help="create the database and load the sample book")
    x.add_argument("--force", action="store_true")
    x.set_defaults(fn=cmd_init)

    x = sub.add_parser("import", help="import holdings from a CSV (ManualBroker)")
    x.add_argument("csv")
    x.add_argument("--merge", action="store_true", help="don't wipe each account's positions first")
    x.set_defaults(fn=cmd_import)

    x = sub.add_parser("catalog", help="load instrument metadata (name, type, sector, mix, fee) from a CSV")
    x.add_argument("csv")
    x.set_defaults(fn=cmd_catalog)

    sub.add_parser("brief", help="one-screen status").set_defaults(fn=cmd_brief)
    for name, fn in (("holdings", cmd_holdings), ("allocation", cmd_allocation)):
        x = sub.add_parser(name)
        x.add_argument("--book", choices=["real", "paper"], default="real")
        x.set_defaults(fn=fn)

    x = sub.add_parser("chat", help="talk to Atlas (or: atlas chat 'one question')")
    x.add_argument("question", nargs="*")
    x.add_argument("--engine", choices=["auto", "claude", "offline"], default="auto")
    x.set_defaults(fn=cmd_chat)

    sub.add_parser("prices", help="refresh prices and FX now").set_defaults(fn=cmd_prices)
    x = sub.add_parser("news", help="fetch headlines for holdings now")
    x.add_argument("--days", type=int, default=2)
    x.set_defaults(fn=cmd_news)

    x = sub.add_parser("rules", help="show or change Risk Officer rules")
    rs = x.add_subparsers(dest="rules_cmd")
    y = rs.add_parser("set")
    y.add_argument("key")
    y.add_argument("value", type=_dec)
    for n in ("enable", "disable"):
        rs.add_parser(n).add_argument("key")
    x.set_defaults(fn=cmd_rules)

    x = sub.add_parser("kill", help="kill switch: stop all trading at once")
    x.add_argument("state", choices=["on", "off"])
    x.add_argument("--reason")
    x.set_defaults(fn=cmd_kill)

    x = sub.add_parser("propose", help="propose a limit order (checked by the Risk Officer)")
    x.add_argument("side", choices=["BUY", "SELL", "buy", "sell"])
    x.add_argument("quantity", type=_dec)
    x.add_argument("ticker", help="e.g. XEQT.TO or NVDA.US")
    x.add_argument("--account", required=True, help="e.g. paper-tfsa")
    x.add_argument("--limit", type=_dec, required=True, help="limit price (max for buys, min for sells)")
    x.add_argument("--why")
    x.add_argument("--report", type=int, action="append", help="report id supporting the trade")
    x.set_defaults(fn=cmd_propose)

    x = sub.add_parser("approve", help="approve a proposal and send it to the paper broker")
    x.add_argument("id", type=int)
    x.set_defaults(fn=cmd_approve)
    x = sub.add_parser("dismiss")
    x.add_argument("id", type=int)
    x.add_argument("--reason")
    x.set_defaults(fn=cmd_dismiss)

    x = sub.add_parser("orders")
    x.add_argument("--limit", type=int, default=10)
    x.set_defaults(fn=cmd_orders)

    x = sub.add_parser("reports", help="list specialist reports")
    x.add_argument("--agent")
    x.add_argument("--subject")
    x.add_argument("--limit", type=int, default=20)
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_reports)

    sub.add_parser("schema", help="print the shared report JSON schema").set_defaults(fn=cmd_schema)

    x = sub.add_parser("paper", help="paper book tools")
    x.add_argument("paper_cmd", choices=["reset"])
    x.set_defaults(fn=cmd_paper)

    x = sub.add_parser("jobs", help="run the scheduler, or one job by hand")
    js = x.add_subparsers(dest="jobs_cmd", required=True)
    js.add_parser("start")
    y = js.add_parser("run")
    y.add_argument("name", choices=["prices", "news", "daily_close"])
    x.set_defaults(fn=cmd_jobs)

    x = sub.add_parser("serve", help="run the local HTTP API")
    x.add_argument("--host", default="127.0.0.1")
    x.add_argument("--port", type=int, default=8787)
    x.set_defaults(fn=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
