"""Company profile + peer list, powering the Description and Peers tabs.

Data is fetched on-demand from sikafinance (primary) with a 60-minute
in-memory TTL. If sikafinance fails, we fall back to afx.kwayisi which
carries less data but a superset of tickers with reliable uptime.
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path
from threading import Lock

from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.services import ratios as ratios_svc
from brvm.services._view import (
    CompanyProfile,
    PeerRow,
    PeerStats,
    PeersView,
    Shareholder,
)
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


def _annotate_with_ratios(peers: list[PeerRow]) -> list[PeerRow]:
    """Attach P/E, ROE, net margin to each peer row.

    Called on Peers-tab render; each ticker triggers one small SQL query
    (list_financials LIMIT 2 + latest quote + company_facts). At ~5 peers
    per sector this stays well under 10ms. Missing ratios leave the field
    as None so the template renders '—'."""
    for p in peers:
        view = ratios_svc.get_latest_ratios(p.ticker)
        if view is None:
            continue
        if view.pe:
            p.pe = view.pe.value
        if view.roe:
            p.roe = view.roe.value
        if view.net_margin:
            p.net_margin = view.net_margin.value
    return peers


def _self_row(ticker: str) -> PeerRow | None:
    """Build a `PeerRow` for the currently-viewed company so the Peers
    tab shows it alongside its peers.

    Reads the same shape sikafinance `parse_secteur` produces so the
    row layout stays consistent: name/country from `securities`, live
    last/day%/volume from the newest `quote_snapshots`. Returns None
    when the ticker isn't in `securities` (would be surprising — the
    route already 404s on unknown tickers — but keep the guard for
    parity with the rest of the module)."""
    ticker = ticker.upper()
    with connect(_db_path()) as conn:
        sec = conn.execute(
            "SELECT ticker, name, country FROM securities WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        if sec is None:
            return None
        quote = conn.execute(
            """
            SELECT last, change_pct, volume
            FROM quote_snapshots
            WHERE ticker = ?
            ORDER BY captured_utc DESC
            LIMIT 1
            """,
            (ticker,),
        ).fetchone()
    return PeerRow(
        ticker=sec["ticker"],
        name=sec["name"],
        country=sec["country"],
        last=quote["last"] if quote else None,
        change_day_pct=quote["change_pct"] if quote else None,
        volume=quote["volume"] if quote else None,
        is_self=True,
    )


_PEER_STAT_FIELDS: tuple[str, ...] = (
    "pe", "roe", "net_margin", "change_ytd_pct", "change_day_pct",
)


def _peer_stats(rows: list[PeerRow]) -> dict[str, PeerStats]:
    """Median + mean per ratio across non-self peers.

    Fields with fewer than 2 samples record `n` but leave median/mean
    None, so the UI can render "n=1" as a warning tag rather than a
    number that could be misread as the sector reference.
    """
    non_self = [p for p in rows if not p.is_self]
    out: dict[str, PeerStats] = {}
    for field_name in _PEER_STAT_FIELDS:
        values = [
            getattr(p, field_name) for p in non_self
            if getattr(p, field_name, None) is not None
        ]
        n = len(values)
        if n >= 2:
            out[field_name] = PeerStats(
                median=statistics.median(values),
                mean=statistics.fmean(values),
                n=n,
            )
        elif n == 1:
            out[field_name] = PeerStats(median=None, mean=None, n=1)
    return out


def get_peers_with_ratios(ticker: str) -> PeersView:
    """`get_peers` plus a ratio annotation pass on every returned peer,
    with the currently-viewed company appended as a `is_self=True` row
    so the table doubles as a self-vs-peers comparison view.

    The peers-cache TTL (60 min) still applies to the sector membership
    lookup — the ratio annotation runs on every request because prices
    (and therefore P/E) move intraday. The self row is also rebuilt on
    every request so its intraday price stays fresh."""
    view = get_peers(ticker)
    # Copy so we don't mutate the cached PeersView shared across requests.
    rows = [p.model_copy() for p in view.peers]
    self_row = _self_row(ticker)
    if self_row is not None:
        rows.append(self_row)
    annotated = _annotate_with_ratios(rows)
    return PeersView(
        sector=view.sector,
        source=view.source,
        peers=annotated,
        stats=_peer_stats(annotated),
    )
