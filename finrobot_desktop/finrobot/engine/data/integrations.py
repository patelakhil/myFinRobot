"""User-facing data integrations ↔ DataProvider names ↔ the data they serve.

The Settings page talks about *integrations* ("FMP", "SEC EDGAR", "Finnhub");
the data layer talks about *providers* (``DataProvider.name``: ``"fmp"``,
``"edgar_tools"``…); pipeline steps talk about *data types*. The partial-refresh
feature needs all three joined:

- which providers a refresh may call live        → ``provider_names``
- which data types a refresh actually re-pulls    → ``refreshed_data_types``
- whether an integration is usable right now      → ``is_configured``
- whether an existing report already used it      → ``artifact_integrations``

Stdlib + DataType only at import time (the routes that use it sit on the
sidecar cold-start path); the edgar identity check is imported lazily.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from finrobot.engine.data.types import DataType


@dataclass(frozen=True)
class IntegrationSpec:
    id: str
    label: str
    providers: tuple[str, ...]
    # Settings attribute whose non-empty value means "enabled". None = always on.
    settings_field: str | None
    # Data types reached through EXPLICIT provider methods rather than the
    # capability chain (DataLayer.fetch_segments / fetch_deep_history go straight
    # to EDGAR; fetch_price_target straight to FMP) — a provider's
    # ``capabilities()`` does not list them, but a refresh of that integration
    # does re-pull them.
    extra_data_types: tuple[DataType, ...] = ()
    # Static fallback capability list, used when the provider is not wired into
    # the live DataLayer (e.g. computing a preview before the engine warmed up).
    static_data_types: tuple[DataType, ...] = ()


INTEGRATIONS: tuple[IntegrationSpec, ...] = (
    IntegrationSpec(
        id="fmp",
        label="Financial Modeling Prep",
        providers=("fmp",),
        settings_field="fmp_api_key",
        extra_data_types=(DataType.FORWARD_ESTIMATES,),
        static_data_types=(
            DataType.FINANCIALS,
            DataType.PRICE,
            DataType.PRICE_RANGE,
            DataType.QUOTE,
            DataType.NEWS,
            DataType.EARNINGS,
            DataType.EARNINGS_TRANSCRIPT,
            DataType.FORWARD_ESTIMATES,
            DataType.PEER_CANDIDATES,
            DataType.DIVIDENDS,
        ),
    ),
    IntegrationSpec(
        id="sec_edgar",
        label="SEC EDGAR",
        providers=("edgar_tools",),
        settings_field="sec_user_agent",
        extra_data_types=(DataType.HISTORICAL, DataType.FILINGS_10K),
        static_data_types=(
            DataType.FILINGS_10K,
            DataType.FILINGS_10Q,
            DataType.FILINGS_8K,
            DataType.XBRL_FACTS,
            DataType.INSIDER_TRADES,
            DataType.INSTITUTIONAL_HOLDINGS,
            DataType.PROXY_STATEMENT,
            DataType.SCHEDULE_13,
            DataType.CEO_CERTIFICATION,
        ),
    ),
    IntegrationSpec(
        id="finnhub",
        label="Finnhub",
        providers=("finnhub",),
        settings_field="finnhub_api_key",
        static_data_types=(DataType.PRICE, DataType.NEWS),
    ),
    IntegrationSpec(
        id="adanos",
        label="Adanos (retail sentiment)",
        providers=("adanos",),
        settings_field="adanos_api_key",
        static_data_types=(DataType.SENTIMENT,),
    ),
    IntegrationSpec(
        id="alpha_vantage",
        label="Alpha Vantage (news sentiment)",
        providers=("news_aggregator",),
        settings_field="alpha_vantage_api_key",
        static_data_types=(DataType.NEWS,),
    ),
    IntegrationSpec(
        id="yfinance",
        label="Yahoo Finance",
        providers=("yfinance",),
        settings_field=None,
        static_data_types=(
            DataType.FINANCIALS,
            DataType.PRICE,
            DataType.PRICE_RANGE,
            DataType.QUOTE,
            DataType.NEWS,
        ),
    ),
)

_BY_ID: dict[str, IntegrationSpec] = {spec.id: spec for spec in INTEGRATIONS}
_BY_PROVIDER: dict[str, IntegrationSpec] = {
    provider: spec for spec in INTEGRATIONS for provider in spec.providers
}


def get_integration(integration_id: str) -> IntegrationSpec | None:
    return _BY_ID.get(integration_id)


def unknown_integrations(ids: Iterable[str]) -> list[str]:
    return sorted({i for i in ids if i not in _BY_ID})


def is_configured(spec: IntegrationSpec, settings: Any) -> bool:
    """Is the integration usable with the CURRENT settings?"""
    if spec.settings_field is None:
        return True
    value = str(getattr(settings, spec.settings_field, "") or "").strip()
    if not value:
        return False
    if spec.id == "sec_edgar":
        # Registered only with a valid 'Name email@domain' identity (factory.py).
        from finrobot.engine.data.providers.edgar_provider import _is_valid_identity

        return bool(_is_valid_identity(value))
    return True


def provider_names(ids: Iterable[str]) -> frozenset[str]:
    names: set[str] = set()
    for integration_id in ids:
        spec = _BY_ID.get(integration_id)
        if spec is not None:
            names.update(spec.providers)
    return frozenset(names)


def refreshed_data_types(ids: Iterable[str], data_layer: Any | None = None) -> frozenset[DataType]:
    """Every data type a refresh of ``ids`` can actually re-pull live.

    Prefers the LIVE capability list of the wired providers (so a provider that
    gains a data type is picked up without touching this module); falls back to
    the static list when the provider isn't wired (engine still warming).
    """
    live_caps: dict[str, set[DataType]] = {}
    for provider in getattr(data_layer, "_providers", None) or []:
        try:
            live_caps[provider.name] = {DataType(c) for c in provider.capabilities()}
        except (ValueError, AttributeError, TypeError):
            continue
    out: set[DataType] = set()
    for integration_id in ids:
        spec = _BY_ID.get(integration_id)
        if spec is None:
            continue
        wired = [live_caps[p] for p in spec.providers if p in live_caps]
        if wired:
            for caps in wired:
                out.update(caps)
        else:
            out.update(spec.static_data_types)
        out.update(spec.extra_data_types)
    return frozenset(out)


INTEGRATIONS_TAG_PREFIX = "integrations:"


def integrations_tag(provider_names_used: Iterable[str]) -> str:
    """The artifact tag recording which integrations were wired for a run."""
    ids = sorted({_BY_PROVIDER[p].id for p in provider_names_used if p in _BY_PROVIDER})
    return INTEGRATIONS_TAG_PREFIX + ",".join(ids)


def artifact_integrations(tags: Iterable[str], data_source: str | None) -> set[str]:
    """Integration ids an existing report was generated WITH.

    New artifacts carry an ``integrations:`` tag (stamped by the runner). Legacy
    artifacts only know their primary financials provider (``inputs.data_source``),
    so that is the best honest evidence: a legacy yfinance-sourced report
    counts as "no FMP" and is a refresh candidate; an FMP-sourced one is not.
    """
    for tag in tags:
        if tag.startswith(INTEGRATIONS_TAG_PREFIX):
            body = tag[len(INTEGRATIONS_TAG_PREFIX) :]
            return {part for part in body.split(",") if part}
    found: set[str] = set()
    source = (data_source or "").strip().lower()
    for provider, spec in _BY_PROVIDER.items():
        if provider in source or spec.id in source:
            found.add(spec.id)
    return found
