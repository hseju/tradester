"""Specialists. Each reads the books (read-only) and files reports in the shared format.

Live in phase 2: Advisor, Market Watch (plus the code-based Bookkeeper and Risk Officer).
"""

from atlas.agents.advisor import Advisor
from atlas.agents.market_watch import MarketWatch

LIVE_AGENTS = ["bookkeeper", "risk_officer", "advisor", "market_watch"]

__all__ = ["LIVE_AGENTS", "Advisor", "MarketWatch"]
