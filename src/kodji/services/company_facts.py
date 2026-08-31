"""Weekly refresh of sikafinance-sourced company facts (Phase 4d).

Reads `shares_outstanding`, `float_pct`, and `market_cap_mxof` off the
sikafinance societe page for every active equity and stamps them onto
`securities`. The ratios engine consumes these directly — nothing else in
the app touches this data.

The sikafinance parser already exists (`sources/sikafinance.parse_societe`)
and is behind a 60-min in-memory cache in `services/company`; here we bypass
the cache and hit the source directly because the point of this pass is
freshness, not a re-read of what the Description tab already fetched.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import httpx

from kodji.config import settings
from kodji.db import connect
from kodji.logging import get
from kodji.sources import sikafinance
from kodji.sources._http import make_client
from kodji.sources._num import parse_number
from kodji.store import securities as sec_repo

log = get(__name__)

# Match sikafinance's "Valorisation de la société" units. The value is
# always followed by "MFCFA" (millions de FCFA) in the fixture; we
# multiply by 1_000_000 to store the raw XOF figure — the ratios engine
# and the peers view both want a plain integer number.
_MCAP_UNIT_RE = re.compile(r"MFCFA|MXOF", re.I)


def _parse_shares(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        # sikafinance renders "100 000 000" (nbsp thousands).
        return int(parse_number(raw))
    except ValueError:
        return None


def _parse_float_pct(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return parse_number(raw)
    except ValueError:
        return None


def _parse_market_cap_xof(raw: str | None) -> float | None:
    """Convert '3 440 000 MFCFA' → 3_440_000_000_000 (raw XOF)."""
    if not raw:
        return None
    is_millions = bool(_MCAP_UNIT_RE.search(raw))
    stripped = _MCAP_UNIT_RE.sub("", raw)
    try:
        value = parse_number(stripped)
    except ValueError:
        return None
    return value * 1_000_000 if is_millions else value


def refresh_all(
    client: httpx.Client | None = None,
    *,
    max_age_days: int = 7,
    limit: int = 200,
    delay_between_requests_s: float = 0.5,
) -> dict[str, int]:
    """Walk every stale equity and refresh its company facts.

    Idempotent within `max_age_days`: a rerun the same day is a no-op
    because `list_stale_company_facts` filters by
    `company_facts_refreshed_utc`. `delay_between_requests_s` keeps us
    polite to sikafinance (same shape as `filings.pull_all`).
    """
    close = client is None
    client = client or make_client()
    counts = {
        "considered": 0,
        "refreshed": 0,
        "no_data": 0,
        "failed": 0,
    }

    db_path = Path(settings.db_path)
    try:
        with connect(db_path) as conn:
            rows = sec_repo.list_stale_company_facts(
                conn, max_age_days=max_age_days, limit=limit
            )
            counts["considered"] = len(rows)

            for row in rows:
                ticker = row["ticker"]
                country = row["country"]
                try:
                    raw = sikafinance.fetch_societe(ticker, country, client=client)
                except httpx.HTTPError as e:
                    log.warning("company-facts: sikafinance fetch failed for %s: %s", ticker, e)
                    counts["failed"] += 1
                    continue

                shares = _parse_shares(raw.get("shares_outstanding"))
                pct = _parse_float_pct(raw.get("float_pct"))
                cap = _parse_market_cap_xof(raw.get("market_cap_mxof"))

                if shares is None and pct is None and cap is None:
                    counts["no_data"] += 1
                    # Still stamp the timestamp so a totally-empty issuer
                    # doesn't get retried on every pass — an operator can
                    # clear `company_facts_refreshed_utc` to force a rerun.
                    sec_repo.update_company_facts(conn, ticker)
                    continue

                sec_repo.update_company_facts(
                    conn, ticker,
                    shares_outstanding=shares,
                    float_pct=pct,
                    market_cap_xof=cap,
                )
                counts["refreshed"] += 1

                if delay_between_requests_s:
                    time.sleep(delay_between_requests_s)
    finally:
        if close:
            client.close()

    log.info("company-facts refresh: %s", counts)
    return counts
