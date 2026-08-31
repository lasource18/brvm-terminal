"""Provider interface + concrete implementations.

Phase 1 default is `ScrapeProvider` (sikafinance A-to-Z + afx.kwayisi
cross-check). `ApiProvider` is a stub that raises — it's here so future
paid-feed integration can slot in without touching the service layer.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from kodji.config import settings
from kodji.logging import get
from kodji.models import DailyBar, IndexLevel, Quote, Security
from kodji.sources import afx_kwayisi, sikafinance
from kodji.sources._http import make_client

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


class FallbackProvider:
    """Composes an API-first provider with a scrape fallback.

    F-13: previously `select_provider` returned the raw `ApiProvider`
    whenever `BRVM_API_*` was configured. Because `ApiProvider` is a
    stub that raises `NotImplementedError`, filling those vars (as
    `env.example` invites) silently killed every ingest cycle — no
    scrape, no data, and combined with F-10 no visible badge either.

    The fallback catches `NotImplementedError` from the primary and
    delegates to the scraper. Real transport errors (`httpx.HTTPError`,
    `ValueError` from a bad response) are NOT caught — those are
    genuine API failures worth surfacing, not "API isn't wired up".
    Logs a warning on the first fallback so an operator notices.
    """

    name = "api+scrape"

    def __init__(self, primary: QuoteProvider, fallback: QuoteProvider) -> None:
        self._primary = primary
        self._fallback = fallback
        self._warned_refresh = False
        self._warned_history: set[str] = set()

    def refresh_securities(self) -> tuple[list[Security], list[Quote], list[IndexLevel]]:
        try:
            return self._primary.refresh_securities()
        except NotImplementedError:
            if not self._warned_refresh:
                log.warning(
                    "provider %s.refresh_securities not implemented; "
                    "falling back to %s", self._primary.name, self._fallback.name,
                )
                self._warned_refresh = True
            return self._fallback.refresh_securities()

    def history(self, ticker: str, country: str | None) -> list[DailyBar]:
        try:
            return self._primary.history(ticker, country)
        except NotImplementedError:
            key = f"{ticker}:{country or ''}"
            if key not in self._warned_history:
                log.warning(
                    "provider %s.history(%s) not implemented; "
                    "falling back to %s", self._primary.name, ticker,
                    self._fallback.name,
                )
                self._warned_history.add(key)
            return self._fallback.history(ticker, country)


def select_provider() -> QuoteProvider:
    """Return the provider dictated by the current settings.

    ScrapeProvider is always safe; ApiProvider is only selected when both
    BRVM_API_BASE and BRVM_API_KEY are non-empty, and is always wrapped
    in `FallbackProvider` so a stubbed / partial API doesn't take
    everything down (F-13).
    """
    if settings.has_api_provider:
        return FallbackProvider(
            primary=ApiProvider(settings.brvm_api_base, settings.brvm_api_key),
            fallback=ScrapeProvider(),
        )
    return ScrapeProvider()
