"""Provider interface + concrete implementations.

Phase 1 default is `ScrapeProvider` (sikafinance A-to-Z + afx.kwayisi
cross-check). `ApiProvider` is a stub that raises — it's here so future
paid-feed integration can slot in without touching the service layer.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from brvm.config import settings
from brvm.logging import get
from brvm.models import DailyBar, IndexLevel, Quote, Security
from brvm.sources import afx_kwayisi, sikafinance
from brvm.sources._http import make_client

log = get(__name__)


class QuoteProvider(Protocol):
    name: str

    def refresh_securities(self) -> tuple[list[Security], list[Quote], list[IndexLevel]]:
        ...

    def history(self, ticker: str, country: str | None) -> list[DailyBar]:
        ...


class ScrapeProvider:
    """Composes sikafinance + afx.kwayisi. Default when no API key is set."""

    name = "scrape"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def _c(self) -> httpx.Client:
        return self._client or make_client()

    def refresh_securities(self) -> tuple[list[Security], list[Quote], list[IndexLevel]]:
        client = self._c()
        owns = self._client is None
        try:
            securities, quotes, indices = sikafinance.fetch_aaz(client)
            # Cross-check with afx.kwayisi: if a ticker exists there but not
            # in our set, log it (Phase 1 doesn't yet reconcile).
            try:
                afx_quotes = afx_kwayisi.fetch_home(client)
                sika_tickers = {s.ticker for s in securities}
                extras = [q.ticker for q in afx_quotes if q.ticker not in sika_tickers]
                if extras:
                    log.info("afx cross-check: %d tickers not on sikafinance aaz: %s",
                             len(extras), ", ".join(extras[:10]))
            except httpx.HTTPError as e:
                log.warning("afx cross-check failed: %s", e)
            return securities, quotes, indices
        finally:
            if owns:
                client.close()

    def history(self, ticker: str, country: str | None) -> list[DailyBar]:
        client = self._c()
        owns = self._client is None
        try:
            try:
                return sikafinance.fetch_historique(ticker, country, client)
            except httpx.HTTPError as e:
                log.warning("sikafinance historique %s failed: %s; trying afx", ticker, e)
                try:
                    _, bars = afx_kwayisi.fetch_ticker(ticker, client)
                    return bars
                except httpx.HTTPError as e2:
                    log.warning("afx ticker %s also failed: %s", ticker, e2)
                    return []
        finally:
            if owns:
                client.close()


class ApiProvider:
    """Stub for a future commercial BRVM feed.

    The Apify-hosted `BRVM Market Data API` referenced in CLAUDE.md was
    withdrawn in June 2026. Once a replacement (EODHD, ICE, or a revived
    endpoint) is wired up here, `services.quotes` will pick it up
    automatically because both providers implement the same protocol.
    """

    name = "api"

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = base_url
        self.api_key = api_key

    def refresh_securities(self) -> tuple[list[Security], list[Quote], list[IndexLevel]]:
        raise NotImplementedError(
            "No working BRVM Market Data API endpoint as of 2026-06 "
            "(previous Apify-hosted service was withdrawn for licensing reasons). "
            "Configure a supported paid provider or leave BRVM_API_KEY empty."
        )

    def history(self, ticker: str, country: str | None) -> list[DailyBar]:
        raise NotImplementedError


def select_provider() -> QuoteProvider:
    """Return the provider dictated by the current settings.

    ScrapeProvider is always safe; ApiProvider is only selected when both
    BRVM_API_BASE and BRVM_API_KEY are non-empty.
    """
    if settings.has_api_provider:
        return ApiProvider(settings.brvm_api_base, settings.brvm_api_key)
    return ScrapeProvider()
