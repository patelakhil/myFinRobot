"""Portfolio Desk — the user's actual holdings (portfolios + purchase lots).

Persistence (:class:`PortfolioStore`) is decoupled from orchestration
(:func:`build_overview` in :mod:`portfolio.service`): the store knows only
portfolios/lots; the service joins them with the DataLayer canonical PRICE for
the live mark and computes market value / P&L. CSV import/export is pure
parsing in :mod:`portfolio.importer`. Same layering as :mod:`finrobot.coverage`
(ADR-0012).
"""

from __future__ import annotations

from finrobot.portfolio.models import (
    ImportPreview,
    ImportRow,
    Lot,
    Portfolio,
    PortfolioDetail,
    PortfolioOverview,
    PortfolioPosition,
    PortfolioSummary,
)
from finrobot.portfolio.sqlite_store import PortfolioStore

__all__ = [
    "ImportPreview",
    "ImportRow",
    "Lot",
    "Portfolio",
    "PortfolioDetail",
    "PortfolioOverview",
    "PortfolioPosition",
    "PortfolioStore",
    "PortfolioSummary",
]
