from __future__ import annotations

from datetime import datetime, timezone

import pytest

from atlas.app import AtlasApp
from atlas.config import Settings
from atlas.prices.sample import SampleAdapter
from atlas.prices.service import PriceService
from atlas.seed import seed_sample

# Tuesday 2026-11-24 10:00 ET: markets open.
OPEN = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, t: datetime):
        self.t = t

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def clock():
    return Clock(OPEN)


@pytest.fixture
def sample(clock):
    return SampleAdapter(now=clock)


@pytest.fixture
def app(tmp_path, clock, sample):
    settings = Settings(db_url=f"sqlite:///{tmp_path}/atlas.db", price_source="sample")
    return AtlasApp(settings, prices=PriceService([sample], [sample], [sample]), now=clock)


@pytest.fixture
def seeded(app):
    with app.services() as s:
        seed_sample(s)
    return app
