"""Phase 4c: sikafinance-communiqué → filings promotion."""

from __future__ import annotations

import hashlib
from datetime import date

import pytest

from brvm.config import reset_settings_cache
from brvm.db import connect
from brvm.models import Filing, NewsItem, Security
from brvm.sources._dedupe import news_hash
from brvm.sources._dedupe import url_hash as compute_url_hash
from brvm.store import filings as filings_repo
from brvm.store import news as news_repo
from brvm.store import securities as sec_repo

from .conftest import apply_migrations

# ---------------------------------------------------------------------------
# classify_communique_title — pure taxonomy tests, no DB.
# ---------------------------------------------------------------------------


def _classify(title: str):
    from brvm.services.filings import classify_communique_title

    return classify_communique_title(title)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        (
            "BOA BF _ Etats financiers - 1er semestre 2026",
            ("etats_financiers", "H1", 2026),
        ),
        (
            "SONATEL SN _ Rapport d'activités - 1er semestre 2026",
            ("rapport_activites", "H1", 2026),
        ),
        (
            "SITAB CI _ Rapport d'activités - 2eme trimestre 2026",
            # Q2 folds into H1 (same six months, different reporting label).
            ("rapport_activites", "H1", 2026),
        ),
        (
            "AFRICA GLOBAL LOGISTICS CI _ Etats financiers - Exercice 2025",
            ("etats_financiers", "annual", 2025),
        ),
        (
            "SONATEL SN _ Rapport annuel 2024",
            ("rapport_annuel", "annual", 2024),
        ),
        (
            "BOA CI _ Rapport d'activités annuel - Exercice 2024",
            ("rapport_annuel", "annual", 2024),
        ),
        (
            "ETI TG _ Rapport d'examen limité sur les informations "
            "financières intermédiaires - 1er semestre 2026",
            ("rapport_activites", "H1", 2026),
        ),
        (
            "BOA ML _ Rapport d'activités - 1er trimestre 2026",
            ("rapport_activites", "Q1", 2026),
        ),
        (
            "BOA BF _ Rapport d'activités - 3eme trimestre 2025",
            ("rapport_activites", "Q3", 2025),
        ),
        (
            "SGBCI _ Communiqué de résultats - Exercice 2025",
            ("resultats", "annual", 2025),
        ),
    ],
)
def test_classifies_filing_titles(title, expected):
    got = _classify(title)
    assert got is not None
    assert (got.doc_type, got.period_kind, got.period_year) == expected
    # Period label should surface a human-readable stem for the filename.
    assert got.period_label


@pytest.mark.parametrize(
    "title",
    [
        "BOA BN _ Notation Financière",
        "SAPH CI _ Paiement de dividendes - Exercice 2025",
        "SONATEL SN _ Communiqué - Changement important dans la Direction",
        "SAFCA CI _ Admission à la cote des actions nouvelles",
        "AFRICA GLOBAL LOGISTICS CI _ Avis de convocation - Assemblée Générale Mixte",
        "AFRICA GLOBAL LOGISTICS CI _ Projet de résolutions - Assemblée Générale Mixte",
        "AFRICA GLOBAL LOGISTICS CI _ Pouvoir - Assemblée Générale Mixte",
        "LNB BN _ Bilan semestriel du contrat de liquidité - 1er semestre 2026",
        "ETI TG _ Communiqué de Presse",
    ],
)
def test_skips_non_filings(title):
    assert _classify(title) is None


def test_safe_file_name_truncates_long_period_labels():
    """The SAPH 2024 CAC row on brvm.org carries a full sentence in the
    period-label slot. Uncapped, the resulting path blows past macOS's
    255-byte cap and `dest.exists()` raises OSError 63."""
    from brvm.services.filings import _MAX_STEM_CHARS, _safe_file_name

    long_label = (
        "rapport des commissaires aux comptes sur l'existence et la tenue "
        "conforme du registre des titres nominatifs émis par la société "
        "établi en application de l'article 746-2 de l'acte uniforme de "
        "l'OHADA relatif au droit des sociétés commerciales et GIE"
    )
    name = _safe_file_name(
        "2024-07-18",
        "autre",
        long_label,
        "https://www.brvm.org/sites/default/files/some-file.pdf",
    )
    stem = name.removesuffix(".pdf")
    assert len(stem) <= _MAX_STEM_CHARS
    # The hash suffix keeps two truncated stems from different URLs
    # distinct.
    name2 = _safe_file_name(
        "2024-07-18",
        "autre",
        long_label,
        "https://www.brvm.org/sites/default/files/other-file.pdf",
    )
    assert name != name2


def test_sikafinance_file_stem_truncates_long_period_labels():
    from brvm.services.filings import _MAX_STEM_CHARS, _sikafinance_file_stem

    long_label = "x" * 500
    stem = _sikafinance_file_stem(
        None, "rapport_activites", long_label,
        url="https://www.sikafinance.com/docs/very-long.pdf",
    )
    assert len(stem) <= _MAX_STEM_CHARS


def test_bare_year_annual_report():
    # No "Exercice" keyword; the "Rapport annuel" branch salvages a year.
    got = _classify("SONATEL SN _ Rapport annuel 2024")
    assert got and (got.doc_type, got.period_kind, got.period_year) == (
        "rapport_annuel",
        "annual",
        2024,
    )


# ---------------------------------------------------------------------------
# promote_from_communiques — end-to-end DB round-trip with a stubbed client
# ---------------------------------------------------------------------------


def _fresh_svc(tmp_path, monkeypatch):
    db_path = tmp_path / "brvm.sqlite"
    filings_dir = tmp_path / "filings"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("FILINGS_ROOT", str(filings_dir))
    reset_settings_cache()
    from brvm.services import filings as svc
    return svc, db_path, filings_dir


class _StubClient:
    """Streams `payload` bytes for every URL; records the requested URLs."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.requested: list[str] = []

    def stream(self, method, url):
        self.requested.append(url)
        payload = self.payload

        class _R:
            def raise_for_status(self):
                pass

            def iter_bytes(self):
                yield payload

            def __enter__(self):
                return self

            def __exit__(self, *_a):
                return False

        return _R()

    def close(self):
        pass


def _seed_news(conn, items: list[NewsItem]) -> None:
    news_repo.upsert_news_items(conn, items)


def _mk_communique(url: str, title: str, issuer: str, published: str) -> NewsItem:
    return NewsItem(
        source="sikafinance",
        kind="communique",
        url=url,
        url_hash=news_hash(url, title),
        title=title,
        issuer_name=issuer,
        published_at=published,
    )


def test_promote_downloads_filings_and_dedupes(monkeypatch, tmp_path, fixtures_dir):
    svc, db_path, filings_dir = _fresh_svc(tmp_path, monkeypatch)
    tiny_pdf = (fixtures_dir / "brvm_org" / "tiny_two_page.pdf").read_bytes()

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
            Security(ticker="BOABF", name="BANK OF AFRICA BURKINA FASO",
                     kind="equity", country="BF"),
        ])
        _seed_news(conn, [
            _mk_communique(
                "https://www.sikafinance.com/docs/snts-h1-2026.pdf",
                "SONATEL SN _ Rapport d'activités - 1er semestre 2026",
                "SONATEL",
                "2026-07-24T00:00:00Z",
            ),
            _mk_communique(
                "https://www.sikafinance.com/docs/boabf-ef-h1-2026.pdf",
                "BOA BF _ Etats financiers - 1er semestre 2026",
                "BANK OF AFRICA BURKINA FASO",
                "2026-08-05T00:00:00Z",
            ),
            # Not a filing: dividend notice — must not be promoted or fetched.
            _mk_communique(
                "https://www.sikafinance.com/docs/saph-div-2025.pdf",
                "SAPH CI _ Paiement de dividendes - Exercice 2025",
                "SAPH CI",
                "2026-08-12T00:00:00Z",
            ),
            # Filing but for an unknown issuer — must be counted, not fetched.
            _mk_communique(
                "https://www.sikafinance.com/docs/unknown-2024.pdf",
                "UNLISTED CO _ Etats financiers - Exercice 2024",
                "UNLISTED CO",
                "2026-02-01T00:00:00Z",
            ),
        ])

    stub = _StubClient(tiny_pdf)
    counts = svc.promote_from_communiques(client=stub, delay_between_requests_s=0)

    assert counts["communiques_seen"] == 4
    assert counts["not_a_filing"] == 1
    assert counts["unresolved_ticker"] == 1
    assert counts["filings_new"] == 2
    assert counts["cross_source_dupe"] == 0
    assert counts["url_dupe"] == 0
    # Only the two filing URLs were fetched — the dividend + unknown were
    # short-circuited before the network call.
    assert sorted(stub.requested) == sorted([
        "https://www.sikafinance.com/docs/snts-h1-2026.pdf",
        "https://www.sikafinance.com/docs/boabf-ef-h1-2026.pdf",
    ])

    # The two files landed under the right ticker directories.
    snts_files = list((filings_dir / "SNTS").glob("*.pdf"))
    boabf_files = list((filings_dir / "BOABF").glob("*.pdf"))
    assert len(snts_files) == 1
    assert len(boabf_files) == 1
    for p in (*snts_files, *boabf_files):
        assert p.name.startswith("sikafinance_")

    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT ticker, doc_type, period_kind, period_year, source, sha256, "
            "size_bytes, page_count, published_date "
            "FROM filings ORDER BY ticker"
        ).fetchall()
    assert [(r["ticker"], r["doc_type"], r["period_kind"], r["period_year"], r["source"])
            for r in rows] == [
        ("BOABF", "etats_financiers", "H1", 2026, "sikafinance"),
        ("SNTS", "rapport_activites", "H1", 2026, "sikafinance"),
    ]
    for r in rows:
        assert r["sha256"] == hashlib.sha256(tiny_pdf).hexdigest()
        assert r["size_bytes"] == len(tiny_pdf)
        assert r["page_count"] == 2
    assert {r["published_date"] for r in rows} == {"2026-07-24", "2026-08-05"}

    # Re-running the promoter must be a full no-op: every URL is now in
    # `filings`, so the LEFT JOIN filters them out at the DB layer.
    stub2 = _StubClient(tiny_pdf)
    counts2 = svc.promote_from_communiques(client=stub2, delay_between_requests_s=0)
    assert counts2["communiques_seen"] == 2  # the 2 non-filings the DB still holds
    assert counts2["filings_new"] == 0
    assert stub2.requested == []


def test_promote_cross_source_dedupe_against_brvm_org(monkeypatch, tmp_path, fixtures_dir):
    """If brvm.org already carries the (ticker, doc_type, period) triple,
    promoting the sikafinance twin must skip without a download."""
    svc, db_path, _filings_dir = _fresh_svc(tmp_path, monkeypatch)
    tiny_pdf = (fixtures_dir / "brvm_org" / "tiny_two_page.pdf").read_bytes()

    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
        ])
        # Pre-existing brvm.org row for SNTS Rapport d'activités H1 2026.
        brvm_url = "https://www.brvm.org/sites/default/files/snts_h1_2026.pdf"
        filings_repo.upsert_filings(conn, [Filing(
            ticker="SNTS",
            issuer_name="SONATEL SN",
            doc_type="rapport_activites",
            period_kind="H1",
            period_year=2026,
            period_label="1er semestre 2026",
            source="brvm_org",
            source_url=brvm_url,
            url_hash=compute_url_hash(brvm_url),
            published_date=date(2026, 7, 24),
            file_path="data/filings/SNTS/brvm.pdf",
            size_bytes=2048,
            sha256="deadbeef" * 8,
            page_count=10,
        )])
        # And the same underlying filing surfacing on sikafinance too.
        _seed_news(conn, [_mk_communique(
            "https://www.sikafinance.com/docs/snts-h1-2026.pdf",
            "SONATEL SN _ Rapport d'activités - 1er semestre 2026",
            "SONATEL",
            "2026-07-24T00:00:00Z",
        )])

    stub = _StubClient(tiny_pdf)
    counts = svc.promote_from_communiques(client=stub, delay_between_requests_s=0)
    assert counts["cross_source_dupe"] == 1
    assert counts["filings_new"] == 0
    assert stub.requested == []


def test_promote_handles_download_failure(monkeypatch, tmp_path):
    """A single broken PDF must not abort the whole pass."""
    import httpx

    svc, db_path, _ = _fresh_svc(tmp_path, monkeypatch)
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
        ])
        _seed_news(conn, [_mk_communique(
            "https://www.sikafinance.com/docs/snts-h1-2026.pdf",
            "SONATEL SN _ Rapport d'activités - 1er semestre 2026",
            "SONATEL",
            "2026-07-24T00:00:00Z",
        )])

    class _FailingClient:
        def stream(self, method, url):
            raise httpx.HTTPError("simulated network failure")

        def close(self):
            pass

    counts = svc.promote_from_communiques(client=_FailingClient(), delay_between_requests_s=0)
    assert counts["failed_download"] == 1
    assert counts["filings_new"] == 0
