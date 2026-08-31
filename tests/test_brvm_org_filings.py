"""Phase 4a: brvm.org filings parsers + downloader + slug resolver."""

from __future__ import annotations

from pathlib import Path

import pytest

from kodji.config import reset_settings_cache
from kodji.db import connect
from kodji.models import Filing, Security
from kodji.sources import brvm_org_filings as bf
from kodji.sources._dedupe import url_hash as compute_url_hash
from kodji.store import filings as filings_repo
from kodji.store import securities as sec_repo
from kodji.store import slugs as slugs_repo

from .conftest import apply_migrations

# --------------------------------------------------------------------------- #
# Parsers                                                                     #
# --------------------------------------------------------------------------- #


def test_parse_issuers_index_yields_slug_and_display_name(fixtures_dir):
    html = (fixtures_dir / "brvm_org" / "rapports_societes_cotees.html").read_text(
        encoding="utf-8"
    )
    entries = bf.parse_issuers_index(html)
    assert len(entries) >= 25
    by_slug = {e.slug: e.display_name for e in entries}
    assert "bank-africa-sn" in by_slug
    assert by_slug["bank-africa-sn"].upper().startswith("BANK OF AFRICA")
    # HTML entity should be decoded through selectolax's .text().
    assert any("BOLLORE" in n.upper() for n in by_slug.values())


def test_parse_issuer_page_extracts_metadata_from_filename(fixtures_dir):
    html = (fixtures_dir / "brvm_org" / "rapports_societe_cotes_sonatel.html").read_text(
        encoding="utf-8"
    )
    filings = bf.parse_issuer_page(html)
    assert len(filings) >= 5

    # The Etats financiers Exercice 2025 row is the anchor case.
    ef = next(
        f for f in filings
        if f.doc_type == "etats_financiers" and f.period_year == 2025
    )
    assert ef.period_kind == "annual"
    assert ef.published_date and ef.published_date.isoformat() == "2026-02-16"
    assert ef.source_url.endswith("_-_sonatel_sn.pdf")
    assert ef.issuer_code and "SONATEL" in ef.issuer_code

    # Rapport d'activités - 1er semestre 2026 -> rapport_activites, H1.
    h1 = next(
        f for f in filings
        if f.doc_type == "rapport_activites" and f.period_kind == "H1"
    )
    assert h1.period_year == 2026

    # Rapport d'activités annuel Exercice 2024 -> rapport_annuel, annual.
    ann = next(
        f for f in filings
        if f.doc_type == "rapport_annuel" and f.period_year == 2024
    )
    assert ann.period_kind == "annual"

    # RSE report should classify as 'rse' even though the filename contains
    # 'rapport'.
    rse = [f for f in filings if f.doc_type == "rse"]
    assert rse and rse[0].period_year == 2023


@pytest.mark.parametrize(
    ("text", "expected_kind", "expected_year"),
    [
        ("rapport_dactivites_-_1er_trimestre_2026_-_sonatel_sn", "Q1", 2026),
        ("rapport_dactivites_-_3eme_trimestre_2025_-_sonatel_sn", "Q3", 2025),
        ("etats_financiers_2025_et_attestation", "annual", 2025),
        ("rapport_dactivites_annuel_-_exercice_2024", "annual", 2024),
        ("some_random_-_document_2027", "annual", 2027),   # bare-year fallback
    ],
)
def test_classify_period(text, expected_kind, expected_year):
    kind, year = bf._classify_period(text)
    assert (kind, year) == (expected_kind, expected_year)


def test_parse_filename_extracts_date():
    got = bf._parse_filename("20260724_-_rapport_dactivites_-_1er_semestre_2026_-_sonatel_sn.pdf")
    assert got["published_date"].isoformat() == "2026-07-24"
    assert got["doc_type"] == "rapport_activites"
    assert got["period_kind"] == "H1"
    assert got["period_year"] == 2026


def test_parse_last_page_index_reads_pager_last(fixtures_dir):
    html = (fixtures_dir / "brvm_org" / "rapports_societe_cotes_sonatel.html").read_text(
        encoding="utf-8"
    )
    # Sonatel's committed fixture advertises 6 pages (indices 0..5).
    assert bf.parse_last_page_index(html) == 5


def test_parse_last_page_index_none_when_unpaginated():
    assert bf.parse_last_page_index("<html><body>no pager here</body></html>") is None
    # Pager present but no page= param -> None (defensive).
    assert (
        bf.parse_last_page_index(
            '<ul class="pagination"><li class="pager-last"><a href="#">last</a></li></ul>'
        )
        is None
    )


def test_fetch_issuer_filings_walks_every_page(fixtures_dir):
    """The pager-last stop was the F-03 regression: without it, only page 0
    (~20 filings) entered the corpus and ~100 older filings were never
    ingested. This test walks a stub that serves distinct rows on pages
    0 and 1 and confirms both land, deduped by source_url."""
    page0_html = (fixtures_dir / "brvm_org" / "rapports_societe_cotes_sonatel.html").read_text(
        encoding="utf-8"
    )
    # Synthesize a page 1 with different PDF URLs so we can prove the walker
    # merged them in (the pager-last href on page 0 says the last page is 5,
    # but the walker uses that as an upper bound — an empty page short-
    # circuits, so a two-page stub is enough to exercise the loop).
    page1_html = """
    <table>
      <tr>
        <td><strong>SONATEL SN : Etats financiers - Exercice 2019</strong></td>
        <td><a href="https://brvm.org/sites/default/files/rapports/20200315_-_etats_financiers_-_exercice_2019_-_sonatel_sn.pdf">Télécharger</a></td>
      </tr>
      <tr>
        <td><strong>SONATEL SN : Rapport annuel - Exercice 2018</strong></td>
        <td><a href="https://brvm.org/sites/default/files/rapports/20190515_-_rapport_dactivites_annuel_-_exercice_2018_-_sonatel_sn.pdf">Télécharger</a></td>
      </tr>
    </table>
    """

    calls: list[str] = []

    class _Response:
        def __init__(self, text: str) -> None:
            self.text = text

        def raise_for_status(self) -> None:
            pass

    class _StubClient:
        def get(self, url: str) -> _Response:
            calls.append(url)
            if "page=" not in url:
                return _Response(page0_html)
            # Beyond page 1 the stub returns an empty rows page — the walker
            # must short-circuit rather than fetch every advertised page.
            if url.endswith("page=1"):
                return _Response(page1_html)
            return _Response("<html><body></body></html>")

        def close(self) -> None:
            pass

    filings = bf.fetch_issuer_filings("sonatel", client=_StubClient())

    urls = {f.source_url for f in filings}
    # Page 0 contributions are still there (the anchor case from
    # test_parse_issuer_page_extracts_metadata_from_filename).
    assert any(u.endswith("_-_sonatel_sn.pdf") and "etats_financiers" in u for u in urls)
    # Page 1's older filings landed too — this is what F-03 restored.
    assert any("exercice_2019" in u for u in urls)
    assert any("exercice_2018" in u for u in urls)
    # And the walker stopped as soon as a page returned no new rows —
    # calls should be page 0, page 1, page 2 (empty, stops) at most.
    assert calls[0].endswith("/sonatel")
    assert calls[1].endswith("page=1")
    assert len(calls) <= 3


def test_fetch_issuer_filings_single_page_no_pager():
    """An unpaginated issuer page must not fire a second HTTP request."""
    single_page_html = """
    <table>
      <tr>
        <td><strong>SONATEL SN : Etats financiers - Exercice 2025</strong></td>
        <td><a href="https://brvm.org/x/20260216_-_etats_financiers_-_exercice_2025_-_sonatel_sn.pdf">Télécharger</a></td>
      </tr>
    </table>
    """
    calls: list[str] = []

    class _Response:
        text = single_page_html

        def raise_for_status(self) -> None:
            pass

    class _StubClient:
        def get(self, url: str) -> _Response:
            calls.append(url)
            return _Response()

        def close(self) -> None:
            pass

    filings = bf.fetch_issuer_filings("sonatel", client=_StubClient())
    assert len(filings) == 1
    # Exactly one GET — the pager-less page must not spawn extra requests.
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Store: filings + slugs                                                      #
# --------------------------------------------------------------------------- #


def _init_db(db_path: Path):
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
        ])


def _mk_filing(url: str, ticker: str = "SNTS", **extra) -> Filing:
    return Filing(
        ticker=ticker,
        source="brvm_org",
        source_url=url,
        url_hash=compute_url_hash(url),
        doc_type=extra.pop("doc_type", "etats_financiers"),
        period_kind=extra.pop("period_kind", "annual"),
        period_year=extra.pop("period_year", 2025),
        period_label=extra.pop("period_label", "Exercice 2025"),
        file_path=extra.pop("file_path", f"data/filings/{ticker}/x.pdf"),
        size_bytes=extra.pop("size_bytes", 2048),
        sha256=extra.pop("sha256", "0" * 64),
        page_count=extra.pop("page_count", 10),
        **extra,
    )


def test_filings_upsert_dedupes_on_url_hash(tmp_db_path):
    _init_db(tmp_db_path)
    a = _mk_filing("https://x/etats-2025.pdf")
    b = _mk_filing("https://x/etats-2024.pdf", period_year=2024)
    with connect(tmp_db_path) as conn:
        ins, dupe = filings_repo.upsert_filings(conn, [a, b])
        assert (ins, dupe) == (2, 0)
        # Re-upserting the same URL leaves the row alone.
        ins2, dupe2 = filings_repo.upsert_filings(conn, [a])
        assert (ins2, dupe2) == (0, 1)
        assert filings_repo.count_all(conn) == 2
        assert filings_repo.exists_url_hash(conn, a.url_hash) is True


def test_list_needing_extraction_defaults_include_annual_and_interim(tmp_db_path):
    """Phase 4c: the default set now includes `rapport_activites` so H1/Q1/Q3
    reports are picked up. `rse`/`assemblee`/`autre` remain excluded."""
    _init_db(tmp_db_path)
    with connect(tmp_db_path) as conn:
        filings_repo.upsert_filings(conn, [
            _mk_filing("https://x/ef.pdf", doc_type="etats_financiers"),
            _mk_filing("https://x/ra.pdf", doc_type="rapport_annuel"),
            _mk_filing("https://x/act.pdf", doc_type="rapport_activites"),
            _mk_filing("https://x/rse.pdf", doc_type="rse"),
            _mk_filing("https://x/agm.pdf", doc_type="assemblee"),
            _mk_filing("https://x/other.pdf", doc_type="autre"),
        ])
        rows = filings_repo.list_needing_extraction(conn)
        assert {r["doc_type"] for r in rows} == {
            "etats_financiers", "rapport_annuel", "rapport_activites",
        }


def test_list_needing_extraction_returns_oldest_first(tmp_db_path):
    """F-07: the extraction queue must serve older filings first so a
    later poorer filing can compose on top of an earlier richer one via
    `replace_period`'s preserve-non-null upsert. Newest-first would flip
    the order and let an older filing's numbers regress the newer read.
    """
    from datetime import date as _date

    _init_db(tmp_db_path)
    with connect(tmp_db_path) as conn:
        filings_repo.upsert_filings(conn, [
            _mk_filing(
                "https://x/ra-2023.pdf",
                doc_type="rapport_annuel",
                published_date=_date(2024, 5, 15),
                period_year=2023,
            ),
            _mk_filing(
                "https://x/ef-2024.pdf",
                doc_type="etats_financiers",
                published_date=_date(2025, 2, 20),
                period_year=2024,
            ),
            _mk_filing(
                "https://x/ra-2024.pdf",
                doc_type="rapport_annuel",
                published_date=_date(2025, 6, 10),
                period_year=2024,
            ),
        ])
        rows = filings_repo.list_needing_extraction(conn)
        # Oldest publication date first; the 2023 rapport is served
        # before either 2024 filing.
        assert [r["source_url"] for r in rows] == [
            "https://x/ra-2023.pdf",
            "https://x/ef-2024.pdf",
            "https://x/ra-2024.pdf",
        ]


def test_slug_map_hit_miss_and_persisted_null(tmp_db_path):
    _init_db(tmp_db_path)
    with connect(tmp_db_path) as conn:
        # First: not seen.
        assert slugs_repo.get(conn, "brvm_org", "sonatel") is None

        slugs_repo.remember(conn, "brvm_org", "sonatel", "SNTS", display_name="SONATEL")
        assert slugs_repo.get_ticker(conn, "brvm_org", "sonatel") == "SNTS"

        # Persist an unresolved slug so the fuzzy matcher isn't retried.
        slugs_repo.remember(conn, "brvm_org", "not-a-listing", None,
                            display_name="Some Odd Name",
                            note="auto: no securities.name match")
        row = slugs_repo.get(conn, "brvm_org", "not-a-listing")
        assert row is not None and row["ticker"] is None

        # A subsequent `remember(..., ticker=None)` must not clobber a
        # non-null resolution (manual overrides survive polls).
        slugs_repo.remember(conn, "brvm_org", "sonatel", None,
                            display_name="SONATEL")
        assert slugs_repo.get_ticker(conn, "brvm_org", "sonatel") == "SNTS"


# --------------------------------------------------------------------------- #
# Service: pull_all + resolve_ticker                                          #
# --------------------------------------------------------------------------- #


def _fresh_svc(tmp_path, monkeypatch):
    db_path = tmp_path / "kodji.sqlite"
    filings_dir = tmp_path / "filings"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("FILINGS_ROOT", str(filings_dir))
    reset_settings_cache()
    from kodji.services import filings as svc
    return svc, db_path, filings_dir


def test_resolve_ticker_uses_slug_map_then_fuzzy(monkeypatch, tmp_path):
    svc, db_path, _ = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
            Security(ticker="SGBC", name="SGBCI", kind="equity", country="CI"),
        ])

        # First call: no slug map entry -> fuzzy hit -> persisted.
        t1 = svc.resolve_ticker(conn, "brvm_org", "sonatel", "SONATEL")
        assert t1 == "SNTS"
        row = slugs_repo.get(conn, "brvm_org", "sonatel")
        assert row["ticker"] == "SNTS"

        # Nonsense slug -> persisted NULL, doesn't retry the fuzzy matcher.
        assert svc.resolve_ticker(conn, "brvm_org", "not-listed",
                                  "Some Random Company") is None
        row2 = slugs_repo.get(conn, "brvm_org", "not-listed")
        assert row2 is not None and row2["ticker"] is None


def test_resolve_ticker_persisted_null_skips_fuzzy_matcher(monkeypatch, tmp_path):
    """F-26: the docstring, phase log, and comment on the neighbouring
    test all claim that a persisted NULL short-circuits — the code
    used to re-run the fuzzy matcher on every poll anyway. Pin the
    intended behaviour: after a persisted NULL, a follow-up call must
    NOT re-invoke the fuzzy resolver."""
    svc, db_path, _ = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
        ])
        # First call: fuzzy runs, returns None, persists NULL.
        assert svc.resolve_ticker(conn, "brvm_org", "unknown-slug",
                                  "Some Random Company") is None
        # Second call: monkeypatch the fuzzy resolver so it BLOWS UP
        # if called. If the second attempt reaches the fuzzy branch,
        # the assertion in `_boom` trips.
        def _boom(*args, **kwargs):
            raise AssertionError("fuzzy resolver must not run on persisted NULL")
        monkeypatch.setattr(svc, "_fuzzy_resolve", _boom)
        assert svc.resolve_ticker(conn, "brvm_org", "unknown-slug",
                                  "Some Random Company") is None


def test_resolve_ticker_persisted_null_still_consults_alias_table(
    monkeypatch, tmp_path
):
    """F-26 companion: a persisted NULL must still allow the manual
    alias table to rescue the slug on a later poll — the alias check
    runs BEFORE the fuzzy skip so operators can drop in a hand-mapped
    entry and pick up previously-unresolved slugs without a DB wipe."""
    svc, db_path, _ = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="XYZC", name="XYZ CI", kind="equity", country="CI"),
        ])
        # Empty alias table on first call — persists NULL.
        monkeypatch.setattr(svc, "_MANUAL_SLUG_ALIASES", {})
        assert svc.resolve_ticker(conn, "brvm_org", "mystery-slug",
                                  "Some Random Company") is None
        # Operator adds a hand mapping and re-polls: alias resolves,
        # fuzzy still doesn't need to run.
        monkeypatch.setattr(svc, "_MANUAL_SLUG_ALIASES", {"mystery-slug": "XYZC"})
        def _boom(*args, **kwargs):
            raise AssertionError("fuzzy resolver must not run on persisted NULL")
        monkeypatch.setattr(svc, "_fuzzy_resolve", _boom)
        assert svc.resolve_ticker(conn, "brvm_org", "mystery-slug",
                                  "Some Random Company") == "XYZC"


def test_resolve_ticker_matches_when_display_name_is_the_ticker(monkeypatch, tmp_path):
    """brvm.org occasionally lists a display name that IS the ticker code
    ("NSBC" for NSIA Banque). The name index alone would miss it — the
    ticker index rescues these."""
    svc, db_path, _ = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="NSBC", name="NSIA BANQUE", kind="equity", country="CI"),
        ])
        assert svc.resolve_ticker(conn, "brvm_org", "nsbc", "NSBC") == "NSBC"


def test_resolve_ticker_uses_manual_slug_aliases(monkeypatch, tmp_path):
    """Rename cases + slug-vs-name mismatches the fuzzy matcher can't
    reach: brvm.org's `bici-ci` maps to BICC, `bollore-transport-logistics`
    maps to SDSC (renamed to AGL), etc."""
    svc, db_path, _ = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="BICC", name="BICICI", kind="equity", country="CI"),
            Security(ticker="SDSC", name="AFRICA GLOBAL LOGISTICS",
                     kind="equity", country="CI"),
            Security(ticker="ETIT", name="ETI TG", kind="equity", country="TG"),
            Security(ticker="LNBB", name="LOTERIE NATIONALE DU BENIN",
                     kind="equity", country="BJ"),
        ])
        # brvm.org display "BICI CI" vs securities.name "BICICI" — no
        # normalized match, alias table wins.
        assert svc.resolve_ticker(conn, "brvm_org", "bici-ci", "BICI CI") == "BICC"
        # Rename: brvm.org still calls it Bolloré, we call it AGL.
        assert (
            svc.resolve_ticker(conn, "brvm_org", "bollore-transport-logistics",
                               "BOLLORE TRANSPORT & LOGISTICS")
            == "SDSC"
        )
        # Abbreviation + rebrand: sikafinance display "ETI TG" happens to
        # match here, but a fuzzy match against "ECOBANK TG" would still
        # fail. Alias is what actually resolves it.
        assert svc.resolve_ticker(conn, "brvm_org", "ecobank-tg", "ECOBANK TG") == "ETIT"
        assert svc.resolve_ticker(conn, "brvm_org", "lnb", "LNB") == "LNBB"


def test_resolve_ticker_manual_alias_is_source_scoped(monkeypatch, tmp_path):
    """Manual aliases must only apply to their source; a sikafinance slug
    that happens to collide with a brvm.org alias key mustn't be rewritten."""
    svc, db_path, _ = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="OTHR", name="SOMETHING ELSE", kind="equity", country="CI"),
        ])
        # `bici-ci` is a brvm.org-only alias; hitting sikafinance with the
        # same slug string shouldn't hijack the resolution.
        assert svc.resolve_ticker(conn, "sikafinance", "bici-ci", "SOMETHING ELSE") == "OTHR"


def test_resolve_ticker_bridges_country_code_differences(monkeypatch, tmp_path):
    """brvm.org writes 'BANK OF AFRICA BN' where sikafinance has
    'BANK OF AFRICA BENIN' (country BJ). The resolver must bridge both
    the alias-vs-ISO gap and the expanded-name-vs-code gap.
    """
    svc, db_path, _ = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="BOAB", name="BANK OF AFRICA BENIN",
                     kind="equity", country="BJ"),
            Security(ticker="BOABF", name="BANK OF AFRICA BURKINA FASO",
                     kind="equity", country="BF"),
            Security(ticker="BOAN", name="BANK OF AFRICA NIGER",
                     kind="equity", country="NE"),
            Security(ticker="BOAC", name="BANK OF AFRICA CI",
                     kind="equity", country="CI"),
        ])

        # brvm.org display -> sikafinance ticker.
        assert svc.resolve_ticker(conn, "brvm_org", "bank-africa-bn",
                                  "BANK OF AFRICA BN") == "BOAB"
        assert svc.resolve_ticker(conn, "brvm_org", "bank-africa-bf",
                                  "BANK OF AFRICA BF") == "BOABF"
        assert svc.resolve_ticker(conn, "brvm_org", "bank-africa-ng",
                                  "BANK OF AFRICA NG") == "BOAN"
        # Same code on both sides still works.
        assert svc.resolve_ticker(conn, "brvm_org", "bank-africa-ci",
                                  "BANK OF AFRICA CI") == "BOAC"


def test_pull_all_downloads_and_persists(monkeypatch, tmp_path, fixtures_dir):
    svc, db_path, filings_dir = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
        ])

    tiny_pdf_bytes = (fixtures_dir / "brvm_org" / "tiny_two_page.pdf").read_bytes()

    # Feed the parsers straight from committed HTML (no network in tests).
    sonatel_html = (fixtures_dir / "brvm_org" / "rapports_societe_cotes_sonatel.html").read_text(
        encoding="utf-8"
    )
    # The index fixture (page=0) doesn't include the 'sonatel' slug on its
    # own — brvm.org paginates 30 issuers per page and sonatel lives on
    # page 2. For this test we care about the download+persist path, so
    # hand-inject one issuer instead of parsing the index.
    monkeypatch.setattr(
        bf, "fetch_issuers_index",
        lambda client=None, max_pages=10: [
            bf.IssuerIndexEntry(slug="sonatel", display_name="SONATEL"),
        ],
    )
    monkeypatch.setattr(
        bf, "fetch_issuer_filings",
        lambda slug, client=None: bf.parse_issuer_page(sonatel_html),
    )

    # Stub the streaming download so we don't hit the network — returns
    # the tiny 2-page fixture bytes for every URL.
    def fake_stream(method, url):
        class _R:
            def raise_for_status(self):
                pass
            def iter_bytes(self):
                yield tiny_pdf_bytes
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        return _R()

    import httpx
    class _StubClient:
        def stream(self, method, url):
            return fake_stream(method, url)
        def close(self):
            pass
    _ = httpx  # keep the import so the module reload sees it

    counts = svc.pull_all(client=_StubClient(), delay_between_requests_s=0)
    assert counts["issuers_seen"] == 1
    assert counts["issuers_resolved"] == 1
    assert counts["filings_seen"] > 0
    assert counts["filings_new"] > 0
    assert counts["filings_dupe"] == 0

    # PDFs actually landed on disk under data/filings/SNTS/…
    ticker_dir = filings_dir / "SNTS"
    assert ticker_dir.exists()
    written = list(ticker_dir.glob("*.pdf"))
    assert len(written) == counts["filings_new"]
    # Sanity: every persisted file matches its recorded sha256/size.
    import hashlib
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT file_path, sha256, size_bytes, page_count FROM filings"
        ).fetchall()
    assert rows
    for r in rows:
        # file_path is stored relative to CWD; resolve against the tmp CWD.
        p = Path(r["file_path"])
        if not p.is_absolute():
            p = tmp_path / p.name  # our stub writes under filings_dir/SNTS/
        # Just check pypdf pulled two pages out of every tiny fixture.
        assert r["page_count"] == 2
        assert r["size_bytes"] == len(tiny_pdf_bytes)
        assert r["sha256"] == hashlib.sha256(tiny_pdf_bytes).hexdigest()

    # Re-running should be a full no-op: every URL is already in `filings`.
    counts2 = svc.pull_all(client=_StubClient(), delay_between_requests_s=0)
    assert counts2["filings_new"] == 0
    assert counts2["filings_dupe"] == counts["filings_new"]


def test_pull_all_honors_only_tickers_filter(monkeypatch, tmp_path):
    """`ONLY_TICKERS=SNTS just filings-pull` — the walk still visits every
    issuer on the index (so slug resolution stays warm), but only tickers
    in the allow-list have their filings pages fetched. Useful for
    backfilling one issuer after a fetcher fix without hammering the
    other 46 pages."""
    svc, db_path, _ = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
            Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
        ])

    monkeypatch.setattr(
        bf, "fetch_issuers_index",
        lambda client=None, max_pages=10: [
            bf.IssuerIndexEntry(slug="sonatel", display_name="SONATEL"),
            bf.IssuerIndexEntry(slug="orange-ci", display_name="ORANGE CI"),
        ],
    )
    fetched: list[str] = []

    def _spy_fetch(slug, client=None):
        fetched.append(slug)
        return []  # no filings to download in this test

    monkeypatch.setattr(bf, "fetch_issuer_filings", _spy_fetch)

    class _NoOpClient:
        def close(self):
            pass

    counts = svc.pull_all(
        client=_NoOpClient(),
        only_tickers={"SNTS"},
        delay_between_requests_s=0,
    )
    # Only SNTS's page was fetched; ORAC was filtered out after slug
    # resolution and never hit the (mocked) network.
    assert fetched == ["sonatel"]
    # Both issuers still counted as seen — the filter operates after
    # resolution so the summary reflects the walk faithfully.
    assert counts["issuers_seen"] == 2
    assert counts["issuers_resolved"] == 1


def test_pull_all_politeness_fires_per_pdf_including_failures(
    monkeypatch, tmp_path, fixtures_dir
):
    """F-27: the previous revision slept ONCE per issuer, so an issuer
    with 20 new PDFs blasted the source 20 times back-to-back; failed
    downloads also skipped the sleep entirely. Both are politeness
    violations. Pin the fix: `time.sleep` fires per-PDF, including on
    the failure path."""
    svc, db_path, _filings_dir = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
        ])

    sonatel_html = (fixtures_dir / "brvm_org" / "rapports_societe_cotes_sonatel.html").read_text(
        encoding="utf-8"
    )
    monkeypatch.setattr(
        bf, "fetch_issuers_index",
        lambda client=None, max_pages=10: [
            bf.IssuerIndexEntry(slug="sonatel", display_name="SONATEL"),
        ],
    )
    monkeypatch.setattr(
        bf, "fetch_issuer_filings",
        lambda slug, client=None: bf.parse_issuer_page(sonatel_html),
    )

    # Force every download to fail so the failure branch runs.
    monkeypatch.setattr(svc, "_download_pdf", lambda *a, **kw: None)

    sleeps: list[float] = []
    monkeypatch.setattr(svc.time, "sleep", lambda s: sleeps.append(s))

    class _StubClient:
        def stream(self, method, url):
            raise AssertionError("_download_pdf was stubbed — no stream should run")
        def close(self):
            pass

    counts = svc.pull_all(client=_StubClient(), delay_between_requests_s=0.5)
    # Every seen PDF trips the failure branch, so `filings_failed_download`
    # equals `filings_seen` — and each one incurs one polite sleep.
    assert counts["filings_seen"] > 1  # sanity: sonatel fixture has several
    assert counts["filings_failed_download"] == counts["filings_seen"]
    assert len(sleeps) == counts["filings_seen"]
    assert all(s == 0.5 for s in sleeps)


def test_pull_all_skips_unresolved_issuers(monkeypatch, tmp_path):
    svc, db_path, _ = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        # No SONATEL row this time -> resolve_ticker returns None.
        sec_repo.upsert(conn, [
            Security(ticker="XXXX", name="UNRELATED CO", kind="equity", country="CI"),
        ])

    monkeypatch.setattr(
        bf, "fetch_issuers_index",
        lambda client=None, max_pages=10: [
            bf.IssuerIndexEntry(slug="sonatel", display_name="SONATEL"),
        ],
    )
    # If the code tried to fetch the issuer page, it'd blow up loudly.
    def boom(*_a, **_k):  # pragma: no cover - guard
        raise AssertionError("should not fetch issuer page for unresolved slug")
    monkeypatch.setattr(bf, "fetch_issuer_filings", boom)

    class _NoOpClient:
        def close(self):
            pass
    counts = svc.pull_all(client=_NoOpClient(), delay_between_requests_s=0)
    assert counts["issuers_unresolved"] == 1
    assert counts["filings_new"] == 0
