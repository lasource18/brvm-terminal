"""Fixture-based tests for sikafinance news / communiqué / dividend parsers.

Reference figures come from tests/fixtures/sikafinance/{actualites_brvm,
communiques_brvm,dividendes}.html captured on 2026-08-20.
"""

from __future__ import annotations

from datetime import date

from kodji.sources.sikafinance import (
    parse_communiques,
    parse_dividendes,
    parse_news_feed,
)


def _read(fixtures_dir, rel: str) -> str:
    return (fixtures_dir / rel).read_text(encoding="utf-8")


class TestParseNewsFeed:
    def test_returns_items(self, fixtures_dir):
        items = parse_news_feed(_read(fixtures_dir, "sikafinance/actualites_brvm.html"))
        assert len(items) >= 8
        for it in items:
            assert it.source == "sikafinance"
            assert it.kind == "news"
            assert it.url.startswith("https://www.sikafinance.com/marches/")
            assert it.title
            assert it.url_hash and len(it.url_hash) == 64

    def test_first_item_has_chapeau_and_iso_utc_timestamp(self, fixtures_dir):
        items = parse_news_feed(_read(fixtures_dir, "sikafinance/actualites_brvm.html"))
        top = items[0]
        assert "Panoro Energy" in top.title
        assert top.chapeau and "Panoro" in top.chapeau
        assert top.published_at == "2026-08-20T13:30:20Z"

    def test_url_hash_is_stable_across_calls(self, fixtures_dir):
        html = _read(fixtures_dir, "sikafinance/actualites_brvm.html")
        first = parse_news_feed(html)
        second = parse_news_feed(html)
        assert [i.url_hash for i in first] == [i.url_hash for i in second]

    def test_url_hashes_are_unique_within_page(self, fixtures_dir):
        items = parse_news_feed(_read(fixtures_dir, "sikafinance/actualites_brvm.html"))
        hashes = [i.url_hash for i in items]
        assert len(hashes) == len(set(hashes))


class TestParseCommuniques:
    def test_returns_rows_with_issuer_and_pdf_url(self, fixtures_dir):
        items = parse_communiques(_read(fixtures_dir, "sikafinance/communiques_brvm.html"))
        assert len(items) >= 15
        for it in items:
            assert it.kind == "communique"
            assert it.url.endswith(".pdf")
            assert it.url.startswith("https://www.sikafinance.com/docs/")
            assert it.issuer_name  # split from "COMPANY : TITLE"
            assert it.title
            assert it.published_at and it.published_at.endswith("T00:00:00Z")

    def test_specific_dividend_row_is_parsed(self, fixtures_dir):
        items = parse_communiques(_read(fixtures_dir, "sikafinance/communiques_brvm.html"))
        totc = next(
            (i for i in items if i.issuer_name == "TOTAL CI" and "dividende" in i.title.lower()),
            None,
        )
        assert totc is not None
        assert totc.published_at == "2026-08-14T00:00:00Z"

    def test_url_hashes_are_unique(self, fixtures_dir):
        items = parse_communiques(_read(fixtures_dir, "sikafinance/communiques_brvm.html"))
        hashes = [i.url_hash for i in items]
        assert len(hashes) == len(set(hashes))


class TestParseDividendes:
    def test_upcoming_dividends_have_tickers(self, fixtures_dir):
        actions = parse_dividendes(_read(fixtures_dir, "sikafinance/dividendes.html"))
        assert len(actions) >= 5
        tickers = {a.ticker for a in actions}
        # SGBCI's 2026-08-21 ex-date rolled off the upstream feed after
        # the coupon detached; the fixture was refreshed 2026-08-27 to
        # capture the resulting shape. SPHC / TTLC / NTLC are the three
        # non-"A préciser" rows expected to persist across a refresh.
        assert {"SPHC", "TTLC", "NTLC"}.issubset(tickers)

    def test_sphc_row_shape(self, fixtures_dir):
        # SAPH CI is the top-of-list dated dividend on the refreshed
        # 2026-08-27 fixture. Ex-date is the coupon detachment date on
        # 2026-08-27; amount and yield are the exchange's published
        # figures verbatim.
        actions = parse_dividendes(_read(fixtures_dir, "sikafinance/dividendes.html"))
        sphc = next(a for a in actions if a.ticker == "SPHC")
        assert sphc.kind == "dividend"
        assert sphc.ex_date == date(2026, 8, 27)
        assert sphc.amount == 489.0
        assert sphc.yield_pct == 5.32
        assert sphc.currency == "XOF"
        assert sphc.source == "sikafinance"

    def test_tbd_rows_have_null_ex_date_and_note(self, fixtures_dir):
        actions = parse_dividendes(_read(fixtures_dir, "sikafinance/dividendes.html"))
        tbd = [a for a in actions if a.ex_date is None]
        assert tbd, "expected at least one 'A préciser' row"
        assert all(a.note and "pr" in a.note.lower() for a in tbd)
