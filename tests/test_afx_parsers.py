from datetime import date

from brvm.sources.afx_kwayisi import (
    _parse_short_money,
    parse_home,
    parse_ticker_page,
)


def _read(fixtures_dir, rel: str) -> str:
    return (fixtures_dir / rel).read_text(encoding="utf-8")


class TestParseHome:
    def test_returns_many_quotes(self, fixtures_dir):
        quotes = parse_home(_read(fixtures_dir, "afx/index.html"))
        assert len(quotes) >= 40

    def test_boac_quote(self, fixtures_dir):
        quotes = parse_home(_read(fixtures_dir, "afx/index.html"))
        boac = next(q for q in quotes if q.ticker == "BOAC")
        assert boac.last == 13450.0
        assert boac.volume == 11767
        assert boac.change_abs == 50.0

    def test_source_tag(self, fixtures_dir):
        quotes = parse_home(_read(fixtures_dir, "afx/index.html"))
        assert all(q.source == "afx_kwayisi" for q in quotes)


class TestParseTickerPage:
    def test_snts_top_quote(self, fixtures_dir):
        q, _bars = parse_ticker_page(_read(fixtures_dir, "afx/snts.html"), "SNTS")
        assert q.ticker == "SNTS"
        assert q.last == 32500.0
        assert q.change_abs == 600.0
        assert q.change_pct == 1.88
        assert q.open == 31990.0
        assert q.volume == 3006
        assert q.turnover is not None and abs(q.turnover - 96_200_000) < 1_000_000

    def test_snts_hist_bars(self, fixtures_dir):
        _, bars = parse_ticker_page(_read(fixtures_dir, "afx/snts.html"), "SNTS")
        assert len(bars) >= 5
        top = bars[0]
        assert top.ticker == "SNTS"
        assert top.session_date == date(2026, 8, 18)
        assert top.close == 32500.0
        assert top.volume == 3006


class TestShortMoney:
    def test_millions(self):
        assert _parse_short_money("96.2M") == 96_200_000
        assert _parse_short_money("3.25T") == 3.25e12

    def test_bare_number(self):
        assert _parse_short_money("1,234") == 1234.0
