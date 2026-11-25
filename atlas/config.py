"""Runtime settings, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_CURRENCY = "CAD"
MARKET_TZ = "America/Toronto"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines; never overrides real env vars."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    db_url: str = "sqlite:///data/atlas.db"
    eodhd_api_key: str | None = None
    finnhub_api_key: str | None = None
    price_source: str = "auto"  # auto | sample
    anthropic_api_key: str | None = None
    model: str = "claude-opus-5-5"
    effort: str = "medium"
    worker_model: str = "claude-opus-5-5"  # specialists' structured calls (run at low effort)
    use_llm_specialists: bool = True  # False: Market Watch uses rules only, even with a key
    trading_broker: str = "paper"
    kill_switch_env: bool = False
    # Price freshness: intraday quotes older than this are stale while markets are open.
    intraday_stale_minutes: int = 30
    # Outside market hours (nights, weekends, a holiday) a close is fine for this long.
    closed_stale_hours: int = 96
    paper_slippage_bps: int = 5
    http_timeout: float = 15.0
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_env(cls, dotenv: Path | None = Path(".env")) -> "Settings":
        if dotenv is not None:
            _load_dotenv(dotenv)
        env = os.environ.get
        return cls(
            db_url=env("ATLAS_DB_URL", "sqlite:///data/atlas.db"),
            eodhd_api_key=env("EODHD_API_KEY") or None,
            finnhub_api_key=env("FINNHUB_API_KEY") or None,
            price_source=env("ATLAS_PRICE_SOURCE", "auto").lower(),
            anthropic_api_key=env("ANTHROPIC_API_KEY") or None,
            model=env("ATLAS_MODEL", "claude-opus-5-5"),
            effort=env("ATLAS_EFFORT", "medium"),
            worker_model=env("ATLAS_WORKER_MODEL", "claude-opus-5-5"),
            use_llm_specialists=_flag("ATLAS_LLM_SPECIALISTS", default=True),
            trading_broker=env("ATLAS_TRADING_BROKER", "paper").lower(),
            kill_switch_env=_flag("ATLAS_KILL_SWITCH"),
        )
