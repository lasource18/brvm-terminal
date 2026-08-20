"""Company profile + peer list, powering the Description and Peers tabs.

Data is fetched on-demand from sikafinance (primary) with a 60-minute
in-memory TTL. If sikafinance fails, we fall back to afx.kwayisi which
carries less data but a superset of tickers with reliable uptime.
"""

from __future__ import annotations

import time
from pathlib import Path
from threading import Lock

from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.services._view import CompanyProfile, PeerRow, PeersView, Shareholder
from brvm.sources import afx_kwayisi, sikafinance

log = get(__name__)

TTL_S = 60 * 60
_desc_cache: dict[str, tuple[float, CompanyProfile | None]] = {}
_peers_cache: dict[str, tuple[float, PeersView]] = {}
_lock = Lock()


def _db_path() -> Path:
    return Path(settings.db_path)


def _country_for(ticker: str) -> str | None:
    with connect(_db_path()) as conn:
        r = conn.execute(
            "SELECT country FROM securities WHERE ticker = ?", (ticker,)
        ).fetchone()
    return r["country"] if r else None


def _from_sikafinance_societe(ticker: str, raw: dict) -> CompanyProfile:
    return CompanyProfile(
        ticker=ticker,
        source="sikafinance",
        description=raw.get("description"),
        address=raw.get("address"),
        phone=raw.get("phone"),
        fax=raw.get("fax"),
        leadership=raw.get("leadership"),
        shares_outstanding=raw.get("shares_outstanding"),
        float_pct=raw.get("float_pct"),
        market_cap=raw.get("market_cap_mxof"),
        shareholders=[
            Shareholder(name=n, pct=p) for n, p in raw.get("shareholders", [])
        ],
    )


def _from_afx_factsheet(ticker: str, fs: dict) -> CompanyProfile:
    return CompanyProfile(
        ticker=ticker,
        source="afx_kwayisi",
        sector=fs.get("sector"),
        industry=fs.get("industry"),
        address=fs.get("address"),
        phone=fs.get("telephone"),
        email=fs.get("email"),
        website=fs.get("website"),
    )


def get_description(ticker: str) -> CompanyProfile | None:
    ticker = ticker.upper()
    now = time.time()
    with _lock:
        cached = _desc_cache.get(ticker)
    if cached and now - cached[0] < TTL_S:
        return cached[1]

    country = _country_for(ticker)
    profile: CompanyProfile | None = None

    try:
        raw = sikafinance.fetch_societe(ticker, country)
        if raw.get("description") or raw.get("address"):
            profile = _from_sikafinance_societe(ticker, raw)
    except Exception as e:
        log.warning("sikafinance societe failed for %s: %s", ticker, e)

    if profile is None:
        try:
            _, _bars = afx_kwayisi.fetch_ticker(ticker)
        except Exception as e:
            log.warning("afx ticker fetch failed for %s: %s", ticker, e)
            _bars = None
        # We need the page HTML, not just the parsed quote — fetch it fresh
        # via the raw client so we can pull the factsheet block.
        try:
            import httpx as _httpx

            from brvm.sources._http import make_client

            with make_client() as client:
                r = client.get(f"{afx_kwayisi.BASE}/brvm/{ticker.lower()}.html")
                r.raise_for_status()
                fs = afx_kwayisi.parse_factsheet(r.text)
                if fs:
                    profile = _from_afx_factsheet(ticker, fs)
        except _httpx.HTTPError as e:
            log.warning("afx factsheet failed for %s: %s", ticker, e)

    with _lock:
        _desc_cache[ticker] = (now, profile)
    return profile


def get_peers(ticker: str) -> PeersView:
    ticker = ticker.upper()
    now = time.time()
    with _lock:
        cached = _peers_cache.get(ticker)
    if cached and now - cached[0] < TTL_S:
        return cached[1]

    country = _country_for(ticker)
    view = PeersView(source="none")

    try:
        raw = sikafinance.fetch_secteur(ticker, country)
        peers = [
            PeerRow(
                ticker=p["ticker"],
                name=p["name"],
                country=p.get("country"),
                last=p.get("last"),
                change_day_pct=p.get("change_day_pct"),
                change_ytd_pct=p.get("change_ytd_pct"),
                volume=p.get("volume"),
            )
            for p in raw.get("peers", [])
            if p["ticker"] != ticker
        ]
        if peers:
            view = PeersView(sector=raw.get("sector"), source="sikafinance", peers=peers)
    except Exception as e:
        log.warning("sikafinance secteur failed for %s: %s", ticker, e)

    if view.source == "none":
        try:
            import httpx as _httpx

            from brvm.sources._http import make_client

            with make_client() as client:
                r = client.get(f"{afx_kwayisi.BASE}/brvm/{ticker.lower()}.html")
                r.raise_for_status()
                comps = afx_kwayisi.parse_competitors(r.text, exclude_ticker=ticker)
                if comps:
                    view = PeersView(
                        source="afx_kwayisi",
                        peers=[
                            PeerRow(
                                ticker=c["ticker"],
                                name=c["name"],
                                last=c.get("last"),
                                change_ytd_pct=c.get("change_ytd_pct"),
                                market_cap=c.get("market_cap"),
                            )
                            for c in comps
                        ],
                    )
        except _httpx.HTTPError as e:
            log.warning("afx competitors failed for %s: %s", ticker, e)

    with _lock:
        _peers_cache[ticker] = (now, view)
    return view


def clear_cache() -> None:
    with _lock:
        _desc_cache.clear()
        _peers_cache.clear()
