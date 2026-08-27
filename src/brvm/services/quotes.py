"""High-level orchestration used by jobs and (later) the UI."""

from __future__ import annotations

from pathlib import Path

from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.services.providers import QuoteProvider, select_provider
from brvm.sources import brvm_org_bonds
from brvm.store import quotes as quotes_repo
from brvm.store import securities as sec_repo

log = get(__name__)


def snapshot_once(provider: QuoteProvider | None = None) -> dict[str, int]:
    """Refresh the securities table + write one round of quote snapshots.

    Returns a small dict of row counts for the caller to log/print.
    """
    provider = provider or select_provider()
    log.info("provider=%s", provider.name)

    securities, quotes, indices = provider.refresh_securities()
    log.info(
        "fetched securities=%d quotes=%d indices=%d",
        len(securities),
        len(quotes),
        len(indices),
    )

    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        n_sec = sec_repo.upsert(conn, securities)
        n_q = quotes_repo.insert_snapshots(conn, quotes)
        n_idx = quotes_repo.upsert_index_levels(conn, indices)

    return {"securities": n_sec, "snapshots": n_q, "indices": n_idx}


def snapshot_bonds_once() -> dict[str, int]:
    """Refresh brvm.org bond listings.

    Bonds don't publish OHLC or turnover — just today's price — so they
    land in `daily_bars.close` alongside equities. The unified prices CTE
    in `services.directory` then surfaces bonds on the /directory page
    with the same period-return machinery.
    """
    securities, bars = brvm_org_bonds.fetch_all_bonds()
    log.info("brvm.org bonds: securities=%d bars=%d", len(securities), len(bars))

    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as conn:
        n_sec = sec_repo.upsert(conn, securities)
        n_bars = quotes_repo.upsert_daily_bars(conn, bars)

    return {"securities": n_sec, "bars": n_bars}


def top_by_turnover(limit: int = 10) -> list[dict]:
    """Return the newest snapshot per ticker, sorted by turnover desc."""
    db_path = Path(settings.db_path)
    with connect(db_path) as conn:
        rows = quotes_repo.latest_snapshot_by_ticker(conn)
        # Enrich with the security name.
        names = {
            r[0]: r[1]
            for r in conn.execute("SELECT ticker, name FROM securities").fetchall()
        }
    out = []
    for r in rows[:limit]:
        out.append(
            {
                "ticker": r["ticker"],
                "name": names.get(r["ticker"], r["ticker"]),
                "last": r["last"],
                "change_pct": r["change_pct"],
                "volume": r["volume"],
                "turnover": r["turnover"],
                "source": r["source"],
            }
        )
    return out
