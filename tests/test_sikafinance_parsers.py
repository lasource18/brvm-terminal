"""Fixture-based tests for sikafinance parsers.

The reference figures come directly from the committed HTML captured on
2026-08-18. If sikafinance restructures, one of these assertions will fail
loudly rather than the code crashing at runtime.
"""

from __future__ import annotations

from datetime import date

from brvm.sources.sikafinance import (
    parse_aaz,
    parse_cotation,
    parse_cotation_meta,
    parse_historique,
)


def _read(fixtures_dir, rel: str) -> str:
    return (fixtures_dir / rel).read_text(encoding="utf-8")


class TestParseAAZ:
    def test_returns_securities_quotes_and_indices(self, fixtures_dir):
        html = _read(fixtures_dir, "sikafinance/aaz.html")
        securities, quotes, indices = parse_aaz(html)
        assert len(securities) >= 45
        assert len(quotes) >= 40
        assert len(indices) >= 5

    def test_index_brvmc_present(self, fixtures_dir):
        html = _read(fixtures_dir, "sikafinance/aaz.html")
        _, _, indices = parse_aaz(html)
        brvmc = next(i for i in indices if i.ticker == "BRVMC")
        assert brvmc.level == 507.13
        assert brvmc.change_pct == 1.16

    def test_equity_snts_present_with_country(self, fixtures_dir):
        html = _read(fixtures_dir, "sikafinance/aaz.html")
        securities, quotes, _ = parse_aaz(html)
        snts_sec = next(s for s in securities if s.ticker == "SNTS")
        assert snts_sec.kind == "equity"
        assert snts_sec.country == "SN"
        assert snts_sec.name == "SONATEL"
        snts_q = next(q for q in quotes if q.ticker == "SNTS")
        assert snts_q.last is not None and snts_q.last > 0
        assert snts_q.volume is not None and snts_q.volume > 0
        assert snts_q.turnover is not None and snts_q.turnover > 0

    def test_orange_ci_ticker_and_country(self, fixtures_dir):
        html = _read(fixtures_dir, "sikafinance/aaz.html")
        securities, _, _ = parse_aaz(html)
        orac = next(s for s in securities if s.ticker == "ORAC")
        assert orac.country == "CI"

    def test_source_url_is_absolute(self, fixtures_dir):
        html = _read(fixtures_dir, "sikafinance/aaz.html")
        securities, _, _ = parse_aaz(html)
        assert all(s.source_url and s.source_url.startswith("https://") for s in securities)


class TestParseCotation:
    def test_snts_prices(self, fixtures_dir):
        html = _read(fixtures_dir, "sikafinance/cotation_SNTS.html")
        q = parse_cotation(html, "SNTS")
        assert q.last == 32500.0
        assert q.open == 31990.0
        assert q.high == 32500.0
        assert q.low == 31990.0
        assert q.prev_close == 31900.0
        assert q.volume == 3006
        assert q.turnover == 97_695_000.0
        assert q.change_pct == 1.88

    def test_index_brvmc_has_last(self, fixtures_dir):
        html = _read(fixtures_dir, "sikafinance/cotation_BRVMC.html")
        q = parse_cotation(html, "BRVMC")
        # BRVMC has no volume table, but should at least yield a last level.
        assert q.last is not None and q.last > 0


class TestParseCotationMeta:
    def test_snts_metadata(self, fixtures_dir):
        html = _read(fixtures_dir, "sikafinance/cotation_SNTS.html")
        meta = parse_cotation_meta(html)
        assert meta["name"] == "SONATEL"
        assert meta["ticker"] == "SNTS"
        assert meta["isin"] == "SN0000000019"
        assert meta["country"] == "SN"


class TestParseHistorique:
    def test_snts_bars(self, fixtures_dir):
        html = _read(fixtures_dir, "sikafinance/historique_SNTS.html")
        bars = parse_historique(html, "SNTS")
        # Historique page returns a rolling window; fixture had ~60 sessions.
        assert len(bars) >= 20
        top = bars[0]
        assert top.ticker == "SNTS"
        assert top.session_date == date(2026, 8, 18)
        assert top.close == 32500.0
        assert top.open == 31990.0
        assert top.high == 32500.0
        assert top.low == 31990.0
        assert top.volume == 3006
        assert top.turnover == 97_695_000.0
        # Ensure chronological order preserved (newest first) and unique dates.
        dates = [b.session_date for b in bars]
        assert dates == sorted(dates, reverse=True)
        assert len(set(dates)) == len(dates)
