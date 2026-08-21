"""Phase 4a: brvm.org filings parsers + downloader + slug resolver."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from brvm.db import connect
from brvm.models import Filing, Security
from brvm.sources import brvm_org_filings as bf
from brvm.sources._dedupe import url_hash as compute_url_hash
from brvm.store import filings as filings_repo
from brvm.store import securities as sec_repo
from brvm.store import slugs as slugs_repo

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


def test_list_needing_extraction_defaults_to_annual_types(tmp_db_path):
    _init_db(tmp_db_path)
    with connect(tmp_db_path) as conn:
        filings_repo.upsert_filings(conn, [
            _mk_filing("https://x/ef.pdf", doc_type="etats_financiers"),
            _mk_filing("https://x/ra.pdf", doc_type="rapport_annuel"),
            _mk_filing("https://x/act.pdf", doc_type="rapport_activites"),
        ])
        rows = filings_repo.list_needing_extraction(conn)
        assert {r["doc_type"] for r in rows} == {"etats_financiers", "rapport_annuel"}


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
    db_path = tmp_path / "brvm.sqlite"
    filings_dir = tmp_path / "filings"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("FILINGS_ROOT", str(filings_dir))
    import brvm.config as cfg
    import brvm.services.filings as svc

    importlib.reload(cfg)
    importlib.reload(svc)
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
