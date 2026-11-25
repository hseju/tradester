"""Load the sample book: Canadian accounts holding Canadian-listed ETFs plus a few stocks.

All of it is illustrative. Replace it with your own holdings via
`atlas import path/to/holdings.csv`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from atlas.app import Services
from atlas.brokers import ManualBroker

SAMPLE_DIR = Path(__file__).parent / "sample_data"

# Target mix (percent of the whole portfolio) and drift bands, in points.
SAMPLE_TARGETS = {
    "ca_equity": ("15", "5"),
    "us_equity": ("48", "5"),
    "intl_equity": ("17", "5"),
    "fixed_income": ("12", "5"),
    "cash": ("8", "5"),
}

SAMPLE_GOALS = [  # id, name, target, date, accounts, priority, monthly contribution
    ("house", "House down payment", "40000", date(2029, 6, 30), ["fhsa"], 1, "800"),
    ("retirement", "Retirement", "1000000", date(2055, 1, 1), ["rrsp", "tfsa"], 2, "750"),
]


def seed_sample(s: Services, holdings_csv: Path | None = None) -> dict:
    s.rules.ensure_defaults()
    n_inst = s.bookkeeper.load_catalog(SAMPLE_DIR / "instruments.csv")
    result = s.bookkeeper.import_from_broker(ManualBroker(holdings_csv or SAMPLE_DIR / "holdings.csv"))
    for bucket, (tgt, band) in SAMPLE_TARGETS.items():
        s.bookkeeper.set_target(bucket, Decimal(tgt), Decimal(band))
    for gid, name, amount, when, accts, prio, monthly in SAMPLE_GOALS:
        s.bookkeeper.upsert_goal(gid, name, Decimal(amount), when, accts, prio, monthly_contribution=Decimal(monthly))
    refresh = s.bookkeeper.refresh_prices(s.prices)
    paper = s.bookkeeper.clone_to_paper()
    s.bookkeeper.snapshot()
    return {
        "instruments": n_inst,
        "accounts": result.accounts,
        "positions": result.positions,
        "prices_updated": len(refresh.updated),
        "price_failures": refresh.failed,
        "paper_accounts": paper,
        "import_flags": [f.message for f in result.flags],
    }
