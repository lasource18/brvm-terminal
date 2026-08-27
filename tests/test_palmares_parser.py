"""Fixture-based tests for the sikafinance palmarès parser.

Reference figures come from the committed HTML captured on 2026-08-18.
The page shows one variation at a time; this fixture is the default
"Hausses" (gainers) view.
"""

from __future__ import annotations

from brvm.sources.sikafinance import PALMARES_VARIATIONS, parse_palmares


def _read(fixtures_dir, rel: str) -> str:
    return (fixtures_dir / rel).read_text(encoding="utf-8")


class TestParsePalmares:
    def test_returns_ranked_rows(self, fixtures_dir):
        rows = parse_palmares(_read(fixtures_dir, "sikafinance/palmares.html"))
        assert len(rows) >= 5

    def test_first_row_is_top_gainer(self, fixtures_dir):
        # Fixture leads with CIEC — the +7.43% gainer at 6,360 XOF.
        rows = parse_palmares(_read(fixtures_dir, "sikafinance/palmares.html"))
        top = rows[0]
        assert top.ticker == "CIEC"
        assert top.last == 6360.0
        assert top.high == 6360.0
        assert top.low == 5925.0
        assert top.volume == 3259
        assert top.change_pct == 7.43
        assert top.source == "sikafinance"

    def test_missing_table_returns_empty(self):
        # Malformed page (no `#tabQuotes`) must not raise — fixture-based
        # tests confirm the happy path; the empty-html path is the
        # graceful degradation.
        assert parse_palmares("<html></html>") == []

    def test_turnover_stays_none(self, fixtures_dir):
        # The palmarès view doesn't publish turnover; the parser must not
        # invent a value from the volume column or leak the change_pct
        # into it.
        rows = parse_palmares(_read(fixtures_dir, "sikafinance/palmares.html"))
        assert all(r.turnover is None for r in rows)


class TestPalmaresVariations:
    def test_covers_gainers_losers_activity(self):
        # The four keys map onto sikafinance's `dlVariation` values so a
        # caller passing "gainers" doesn't need to know the French label.
        assert PALMARES_VARIATIONS["gainers"] == "h"
        assert PALMARES_VARIATIONS["losers"] == "b"
        assert PALMARES_VARIATIONS["most_active"] == "c"
        assert PALMARES_VARIATIONS["top_volume"] == "v"
