"""Phase 4d: company-facts refresh job + parsers.

The three string→number helpers are what turn sikafinance's
"100 000 000" / "22,47%" / "3 440 000 MFCFA" into stored numbers, so
they're the risky part. Everything else is bookkeeping.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from brvm.config import reset_settings_cache
from brvm.db import connect
from brvm.models import Security
from brvm.services import company_facts
from brvm.store import securities as sec_repo

from .conftest import apply_migrations

# ---------------------------------------------------------------------------
# Parser edge cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("100 000 000", 100_000_000),
        ("100\xa0000\xa0000", 100_000_000),   # actual nbsp from sikafinance
        ("12345", 12_345),
        (None, None),
        ("", None),
        ("-", None),
    ],
)
def test_parse_shares_handles_nbsp_and_missing(raw, expected):
    assert company_facts._parse_shares(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("22,47%", 22.47),
        ("100%", 100.0),
        ("0,5", 0.5),
        (None, None),
        ("A préciser", None),
    ],
)
def test_parse_float_pct(raw, expected):
    got = company_facts._parse_float_pct(raw)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # "MFCFA" == millions de FCFA → multiply by 1e6.
        ("3 440 000 MFCFA", 3_440_000 * 1_000_000),
        ("3\xa0440\xa0000 MFCFA", 3_440_000 * 1_000_000),
        ("125 MXOF", 125 * 1_000_000),
        # No unit → treat as raw XOF (uncommon on sikafinance but possible).
        ("125000000", 125_000_000),
        (None, None),
        ("", None),
    ],
)
def test_parse_market_cap_xof(raw, expected):
    got = company_facts._parse_market_cap_xof(raw)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Refresh flow
# ---------------------------------------------------------------------------


def _fresh(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from brvm.services import company_facts as svc
    return svc, db_path


def _seed(db_path: Path, tickers: list[Security]) -> None:
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, tickers)


def test_refresh_all_persists_shares_float_and_market_cap(monkeypatch, tmp_path):
    svc, db_path = _fresh(monkeypatch, tmp_path)
    _seed(db_path, [
        Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
    ])

    # Stub sikafinance.fetch_societe so we don't touch the network.
    def _fake_fetch(ticker, country, client=None):
        return {
            "shares_outstanding": "100 000 000",
            "float_pct": "22,47%",
            "market_cap_mxof": "3 440 000 MFCFA",
        }

    monkeypatch.setattr(svc.sikafinance, "fetch_societe", _fake_fetch)

    class _NoOpClient:
        def close(self):
            pass

    counts = svc.refresh_all(client=_NoOpClient(), delay_between_requests_s=0)
    assert counts == {"considered": 1, "refreshed": 1, "no_data": 0, "failed": 0}

    with connect(db_path) as conn:
        facts = sec_repo.get_company_facts(conn, "SNTS")
    assert facts["shares_outstanding"] == 100_000_000
    assert facts["float_pct"] == pytest.approx(22.47)
    assert facts["market_cap_xof"] == pytest.approx(3_440_000 * 1_000_000)
    assert facts["company_facts_refreshed_utc"] is not None


def test_refresh_all_skips_recently_refreshed_rows(monkeypatch, tmp_path):
    """A second run within `max_age_days` must be a no-op — sikafinance
    shouldn't get hammered on a scheduler restart."""
    svc, db_path = _fresh(monkeypatch, tmp_path)
    _seed(db_path, [
        Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
    ])

    def _fake_fetch(ticker, country, client=None):
        return {
            "shares_outstanding": "100 000 000",
            "float_pct": "22,47%",
            "market_cap_mxof": "3 440 000 MFCFA",
        }

    calls = {"n": 0}

    def _counting(*a, **kw):
        calls["n"] += 1
        return _fake_fetch(*a, **kw)

    monkeypatch.setattr(svc.sikafinance, "fetch_societe", _counting)

    class _NoOpClient:
        def close(self):
            pass

    svc.refresh_all(client=_NoOpClient(), delay_between_requests_s=0)
    assert calls["n"] == 1

    # Second pass — nothing stale, no fetches.
    counts2 = svc.refresh_all(client=_NoOpClient(), delay_between_requests_s=0)
    assert counts2["considered"] == 0
    assert calls["n"] == 1


def test_refresh_all_stamps_no_data_rows_so_they_arent_retried(monkeypatch, tmp_path):
    """Some sikafinance societe pages return nothing usable (bond issuers,
    OPCVM, orphaned tickers). We still bump `refreshed_utc` so the row
    doesn't get re-fetched on the next weekly pass."""
    svc, db_path = _fresh(monkeypatch, tmp_path)
    _seed(db_path, [
        Security(ticker="XXXX", name="EMPTY CO", kind="equity", country="CI"),
    ])

    def _fake_empty(ticker, country, client=None):
        return {"shares_outstanding": None, "float_pct": None, "market_cap_mxof": None}

    monkeypatch.setattr(svc.sikafinance, "fetch_societe", _fake_empty)

    class _NoOpClient:
        def close(self):
            pass

    counts = svc.refresh_all(client=_NoOpClient(), delay_between_requests_s=0)
    assert counts["no_data"] == 1
    assert counts["refreshed"] == 0
    with connect(db_path) as conn:
        facts = sec_repo.get_company_facts(conn, "XXXX")
    assert facts["shares_outstanding"] is None
    # Stamped so the next run skips it.
    assert facts["company_facts_refreshed_utc"] is not None


def test_refresh_all_survives_http_errors(monkeypatch, tmp_path):
    svc, db_path = _fresh(monkeypatch, tmp_path)
    _seed(db_path, [
        Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
        Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
    ])

    def _flaky(ticker, country, client=None):
        if ticker == "SNTS":
            raise httpx.HTTPError("simulated timeout")
        return {
            "shares_outstanding": "50 000 000",
            "float_pct": "18,0%",
            "market_cap_mxof": "800 000 MFCFA",
        }

    monkeypatch.setattr(svc.sikafinance, "fetch_societe", _flaky)

    class _NoOpClient:
        def close(self):
            pass

    counts = svc.refresh_all(client=_NoOpClient(), delay_between_requests_s=0)
    assert counts["failed"] == 1
    assert counts["refreshed"] == 1

    with connect(db_path) as conn:
        # Failed row wasn't stamped — will be retried on next pass.
        snts = sec_repo.get_company_facts(conn, "SNTS")
        orac = sec_repo.get_company_facts(conn, "ORAC")
    assert snts["shares_outstanding"] is None
    assert snts["company_facts_refreshed_utc"] is None
    assert orac["shares_outstanding"] == 50_000_000
