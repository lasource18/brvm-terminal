"""Enrich the `securities` table with fields that don't come from the
primary bourse fetch.

Currently: `sector`. Data is scraped per-ticker from sikafinance
(`/marches/secteur/<T>.<cc>`) with a small per-request sleep. The afx
factsheet is used as a fallback when sikafinance has no sector row.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.sources import afx_kwayisi, sikafinance
from brvm.sources._http import make_client
from brvm.store import securities as sec_repo

log = get(__name__)

# Small delay between per-ticker requests so we don't hammer sikafinance.
# 47 equities at 0.6s ~= 30s -- well within polite-scrape budget.
_REQUEST_DELAY_S = 0.6


def _sector_from_sikafinance(ticker: str, country: str | None, client: httpx.Client) -> str | None:
    try:
        raw = sikafinance.fetch_secteur(ticker, country, client=client)
    except httpx.HTTPError as e:
        log.warning("sikafinance secteur failed for %s: %s", ticker, e)
        return None
    sector = (raw.get("sector") or "").strip()
    return sector or None


def _sector_from_afx(ticker: str, client: httpx.Client) -> str | None:
    try:
        r = client.get(f"{afx_kwayisi.BASE}/brvm/{ticker.lower()}.html")
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("afx factsheet failed for %s: %s", ticker, e)
        return None
    fs = afx_kwayisi.parse_factsheet(r.text)
    sector = (fs.get("sector") or "").strip()
    return sector or None


def enrich_sectors(
    limit: int | None = None, sleep_s: float = _REQUEST_DELAY_S
) -> dict[str, int]:
    """Backfill `securities.sector` for equities that are missing it.

    Returns counts: candidates seen, updated, still missing.
    """
    db_path = Path(settings.db_path)
    with connect(db_path) as conn:
        candidates = sec_repo.list_missing_sector(conn)

    if limit is not None:
        candidates = candidates[:limit]

    if not candidates:
        return {"candidates": 0, "updated": 0, "still_missing": 0}

    updated = 0
    with make_client() as client, connect(db_path) as conn:
        for i, row in enumerate(candidates):
            ticker = row["ticker"]
            country = row["country"]
            sector = _sector_from_sikafinance(ticker, country, client)
            if not sector:
                sector = _sector_from_afx(ticker, client)
            if sector:
                sec_repo.update_sector(conn, ticker, sector)
                updated += 1
                log.info("sector %s = %r", ticker, sector)
            else:
                log.info("sector %s: no data from any source", ticker)
            if sleep_s and i < len(candidates) - 1:
                time.sleep(sleep_s)

    return {
        "candidates": len(candidates),
        "updated": updated,
        "still_missing": len(candidates) - updated,
    }
