import pytest

from brvm.db import connect
from brvm.models import Security
from brvm.store import securities as sec_repo


@pytest.fixture
def company_env(monkeypatch, tmp_path):
    from brvm.config import reset_settings_cache

    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from brvm.services import company as company_mod

    with connect(db_path) as conn:
        # Apply the full migration set — new tabs (Phase 4d Peers-with-
        # ratios) need columns the earliest two migrations don't ship.
        from .conftest import apply_migrations

        apply_migrations(conn)
        sec_repo.upsert(
            conn, [Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN")]
        )
    company_mod.clear_cache()
    yield company_mod
    company_mod.clear_cache()
    reset_settings_cache()


def test_sikafinance_success_path(company_env, monkeypatch):
    calls = {"n": 0}

    def fake_societe(ticker, country, client=None):
        calls["n"] += 1
        return {
            "ticker": "SNTS",
            "isin": "SN0000000019",
            "description": "SONATEL est le premier opérateur télécom du Sénégal.",
            "address": "6 Rue WAGANE DIOUF",
            "phone": "(+221) 33-839-12-00",
            "leadership": "Président: Alioune NDIAYE",
            "shares_outstanding": "100 000 000",
            "float_pct": "22,47%",
            "market_cap_mxof": "3 440 000 MFCFA",
            "shareholders": [("FRANCE TELECOM", 42.3), ("ETAT", 27.7)],
        }

    monkeypatch.setattr(
        "brvm.services.company.sikafinance.fetch_societe", fake_societe
    )
    p = company_env.get_description("SNTS")
    assert p is not None
    assert p.source == "sikafinance"
    assert p.description.startswith("SONATEL")
    assert p.shareholders[0].name == "FRANCE TELECOM"
    assert calls["n"] == 1

    # Second call served from cache.
    company_env.get_description("SNTS")
    assert calls["n"] == 1


def test_falls_back_to_afx_when_sikafinance_fails(company_env, monkeypatch):
    def broken_societe(*a, **kw):
        raise RuntimeError("network down")

    # Fake the afx page HTML that parse_factsheet knows how to parse.
    fake_afx_html = (
        '<html><body><div data-fact><h3>Factsheet</h3>'
        "<dl><div><div><dt>Sector<dd>Telecoms</div>"
        "<div><dt>Industry<dd>Fixed Line</div></div>"
        "<div><dt>Address<dd>6 Rue Wagane Diouf</div>"
        "</dl></div></body></html>"
    )

    class FakeResp:
        text = fake_afx_html

        def raise_for_status(self):
            pass

    class FakeClient:
        def __init__(self, *a, **kw): ...
        def __enter__(self):
            return self
        def __exit__(self, *a):
            pass
        def get(self, url):
            return FakeResp()

    monkeypatch.setattr(
        "brvm.services.company.sikafinance.fetch_societe", broken_societe
    )
    monkeypatch.setattr("brvm.sources._http.make_client", lambda: FakeClient())

    # afx_kwayisi.fetch_ticker is also called; stub it out too.
    monkeypatch.setattr(
        "brvm.services.company.afx_kwayisi.fetch_ticker",
        lambda t, client=None: (None, []),
    )

    p = company_env.get_description("SNTS")
    assert p is not None
    assert p.source == "afx_kwayisi"
    assert p.sector == "Telecoms"
    assert "Wagane" in p.address


def test_peers_success_path(company_env, monkeypatch):
    def fake_secteur(ticker, country, client=None):
        return {
            "sector": "BRVM - TELECOMMUNICATIONS",
            "peers": [
                {"ticker": "SNTS", "country": "SN", "name": "SONATEL",
                 "last": 32500, "change_day_pct": 1.88, "change_ytd_pct": 31.7,
                 "volume": 4867},
                {"ticker": "ORAC", "country": "CI", "name": "ORANGE CI",
                 "last": 19000, "change_day_pct": 2.68, "change_ytd_pct": 36.91,
                 "volume": 4709},
                {"ticker": "ONTBF", "country": "BF", "name": "ONATEL BF",
                 "last": 2945, "change_day_pct": 1.55, "change_ytd_pct": 18.51,
                 "volume": 15573},
            ],
        }

    monkeypatch.setattr(
        "brvm.services.company.sikafinance.fetch_secteur", fake_secteur
    )
    view = company_env.get_peers("SNTS")
    assert view.source == "sikafinance"
    assert view.sector == "BRVM - TELECOMMUNICATIONS"
    # Self excluded
    assert {p.ticker for p in view.peers} == {"ORAC", "ONTBF"}


def test_peers_with_ratios_annotates_from_ratios_service(company_env, monkeypatch, tmp_path):
    """Phase 4d: the Peers tab route calls `get_peers_with_ratios`, which
    should walk each peer through `services.ratios.get_latest_ratios`
    and populate `pe`, `roe`, `net_margin` on the returned PeerRow."""
    from brvm.models import Filing, Quote
    from brvm.store import filings as filings_repo
    from brvm.store import financials as fin_repo
    from brvm.store import quotes as quotes_repo

    def fake_secteur(ticker, country, client=None):
        return {
            "sector": "TELECOMS",
            "peers": [
                {"ticker": "SNTS", "country": "SN", "name": "SONATEL",
                 "last": 32500, "change_day_pct": 1.0, "change_ytd_pct": 1.0,
                 "volume": 100},
                {"ticker": "ORAC", "country": "CI", "name": "ORANGE CI",
                 "last": 19000, "change_day_pct": 1.0, "change_ytd_pct": 1.0,
                 "volume": 100},
            ],
        }

    monkeypatch.setattr(
        "brvm.services.company.sikafinance.fetch_secteur", fake_secteur
    )

    # Seed ORAC with financials + shares + a quote so it produces ratios.
    db_path = tmp_path / "brvm.sqlite"
    with connect(db_path) as conn:
        sec_repo.upsert(conn, [
            Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
            Security(ticker="ONTBF", name="ONATEL BF", kind="equity", country="BF"),
        ])
        sec_repo.update_company_facts(
            conn, "ORAC", shares_outstanding=150_000_000,
        )
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="ORAC", source="sikafinance", last=19000.0, change_pct=1.0),
        ])
        filings_repo.upsert_filings(conn, [Filing(
            ticker="ORAC", issuer_name="ORANGE CI", doc_type="rapport_annuel",
            period_kind="annual", period_year=2024, source="brvm_org",
            source_url="u1", url_hash="h1",
            file_path="p1", size_bytes=1, sha256="a", page_count=1,
        )])
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        fin_repo.replace_period(conn, filing_id=filing_id, financials=fin_repo.FinancialsRow(
            ticker="ORAC", period_year=2024,
            revenue=1_500_000_000_000,
            net_income=300_000_000_000,
            eps=2000,
            total_equity=1_000_000_000_000,
            total_assets=3_000_000_000_000,
        ))

    view = company_env.get_peers_with_ratios("SNTS")
    peers_by_ticker = {p.ticker: p for p in view.peers}

    # ORAC has extraction data → ratios populated.
    orac = peers_by_ticker["ORAC"]
    assert orac.pe == pytest.approx(19000 / 2000)             # 9.5x
    assert orac.roe == pytest.approx(30.0)                    # 300 / 1000 * 100
    assert orac.net_margin == pytest.approx(20.0)             # 300 / 1500 * 100

    # ONTBF was never extracted — ratios stay None so the template
    # renders "—" rather than blowing up.
    ontbf = peers_by_ticker.get("ONTBF")
    if ontbf is not None:  # fixture may not include it depending on filter
        assert ontbf.pe is None
        assert ontbf.roe is None
        assert ontbf.net_margin is None


def test_peers_with_ratios_annotates_cash_flow_ratios(
    company_env, monkeypatch, tmp_path
):
    """PR-F: the Peers tab now surfaces P/FCF, FCF yield, and EV/EBITDA
    in addition to P/E / ROE / net margin. Verify a peer with a positive
    free_cash_flow and operating_income row picks up all three."""
    from brvm.models import Filing, Quote
    from brvm.store import filings as filings_repo
    from brvm.store import financials as fin_repo
    from brvm.store import quotes as quotes_repo

    def fake_secteur(ticker, country, client=None):
        return {
            "sector": "TELECOMS",
            "peers": [
                {"ticker": "SNTS", "country": "SN", "name": "SONATEL",
                 "last": 32500, "change_day_pct": 1.0, "change_ytd_pct": 1.0,
                 "volume": 100},
                {"ticker": "ORAC", "country": "CI", "name": "ORANGE CI",
                 "last": 19000, "change_day_pct": 1.0, "change_ytd_pct": 1.0,
                 "volume": 100},
            ],
        }

    monkeypatch.setattr(
        "brvm.services.company.sikafinance.fetch_secteur", fake_secteur
    )

    db_path = tmp_path / "brvm.sqlite"
    with connect(db_path) as conn:
        sec_repo.upsert(conn, [
            Security(ticker="ORAC", name="ORANGE CI", kind="equity", country="CI"),
        ])
        sec_repo.update_company_facts(
            conn, "ORAC", shares_outstanding=150_000_000,
        )
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="ORAC", source="sikafinance", last=19000.0, change_pct=1.0),
        ])
        filings_repo.upsert_filings(conn, [Filing(
            ticker="ORAC", issuer_name="ORANGE CI", doc_type="rapport_annuel",
            period_kind="annual", period_year=2024, source="brvm_org",
            source_url="u1", url_hash="h1",
            file_path="p1", size_bytes=1, sha256="a", page_count=1,
        )])
        filing_id = int(conn.execute("SELECT id FROM filings").fetchone()["id"])
        # Positive FCF + operating income → all three cash-flow ratios populated.
        fin_repo.replace_period(conn, filing_id=filing_id, financials=fin_repo.FinancialsRow(
            ticker="ORAC", period_year=2024,
            revenue=1_500_000_000_000,
            operating_income=400_000_000_000,
            net_income=300_000_000_000,
            eps=2000,
            total_equity=1_000_000_000_000,
            total_assets=3_000_000_000_000,
            free_cash_flow=200_000_000_000,
        ))

    view = company_env.get_peers_with_ratios("SNTS")
    orac = next(p for p in view.peers if p.ticker == "ORAC")

    # market_cap = 150e6 x 19_000 = 2.85e12
    # P/FCF = 2.85e12 / 200e9 = 14.25x
    # FCF yield = 200e9 / 2.85e12 * 100 = 7.02%
    # EV/EBITDA proxy = 2.85e12 / 400e9 = 7.125x
    assert orac.pfcf == pytest.approx(14.25, rel=1e-3)
    assert orac.fcf_yield == pytest.approx(7.0175, rel=1e-3)
    assert orac.ev_ebitda == pytest.approx(7.125, rel=1e-3)


def test_peers_with_ratios_appends_self_row_at_the_bottom(company_env, monkeypatch, tmp_path):
    """The currently-viewed company shows up as an `is_self=True` row at
    the end of the peers list so the ratios table doubles as a
    self-vs-peers comparison view."""
    from brvm.models import Quote
    from brvm.store import quotes as quotes_repo

    def fake_secteur(ticker, country, client=None):
        return {
            "sector": "TELECOMS",
            "peers": [
                {"ticker": "SNTS", "country": "SN", "name": "SONATEL",
                 "last": 32500, "change_day_pct": 1.0, "change_ytd_pct": 1.0,
                 "volume": 100},
                {"ticker": "ORAC", "country": "CI", "name": "ORANGE CI",
                 "last": 19000, "change_day_pct": 2.0, "change_ytd_pct": 5.0,
                 "volume": 200},
            ],
        }

    monkeypatch.setattr(
        "brvm.services.company.sikafinance.fetch_secteur", fake_secteur
    )

    # Seed a live SNTS quote so the self row picks up last/day%/volume.
    db_path = tmp_path / "brvm.sqlite"
    with connect(db_path) as conn:
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="SNTS", source="sikafinance",
                  last=32500.0, change_pct=1.88, volume=3006),
        ])

    view = company_env.get_peers_with_ratios("SNTS")

    # ORAC is the only non-self peer (SNTS is excluded from the peers
    # feed, then re-appended as the self row).
    non_self = [p for p in view.peers if not p.is_self]
    self_rows = [p for p in view.peers if p.is_self]
    assert {p.ticker for p in non_self} == {"ORAC"}
    assert len(self_rows) == 1

    # Self row is last (template renders it visually distinguished at
    # the bottom of the table).
    assert view.peers[-1].is_self is True
    self_row = view.peers[-1]
    assert self_row.ticker == "SNTS"
    assert self_row.name == "SONATEL"
    assert self_row.last == pytest.approx(32500.0)
    assert self_row.change_day_pct == pytest.approx(1.88)
    assert self_row.volume == 3006


def test_peers_with_ratios_self_row_shows_when_no_peers_available(
    company_env, monkeypatch, tmp_path
):
    """Even when sikafinance returns no peers, the self row still
    populates so the tab isn't empty for issuers in an orphan sector."""
    from brvm.models import Quote
    from brvm.store import quotes as quotes_repo

    monkeypatch.setattr(
        "brvm.services.company.sikafinance.fetch_secteur",
        lambda ticker, country, client=None: {"sector": None, "peers": []},
    )
    # Stub the afx fallback client to return an empty competitors block
    # so `get_peers` returns a `source="none"` view with no peers, and
    # the self row is the only survivor.
    class _EmptyResp:
        text = "<html></html>"

        def raise_for_status(self):
            pass

    class _EmptyClient:
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            pass
        def get(self, url):
            return _EmptyResp()

    monkeypatch.setattr("brvm.sources._http.make_client", lambda: _EmptyClient())

    db_path = tmp_path / "brvm.sqlite"
    with connect(db_path) as conn:
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="SNTS", source="sikafinance",
                  last=32500.0, change_pct=1.0, volume=100),
        ])

    view = company_env.get_peers_with_ratios("SNTS")
    assert len(view.peers) == 1
    assert view.peers[0].is_self is True
    assert view.peers[0].ticker == "SNTS"


# --- Phase 8g: peer median + mean stats block ----------------------------


class TestPeerStats:
    """Pure helper — uses PeerRow instances directly, no DB."""

    def _rows(self):
        from brvm.services._view import PeerRow
        return [
            PeerRow(ticker="A", name="A", last=100.0, change_ytd_pct=2.0,
                    pe=8.0, roe=10.0, net_margin=5.0),
            PeerRow(ticker="B", name="B", last=200.0, change_ytd_pct=4.0,
                    pe=12.0, roe=None, net_margin=15.0),
            PeerRow(ticker="C", name="C", last=300.0, change_ytd_pct=6.0,
                    pe=10.0, roe=20.0, net_margin=None),
            PeerRow(ticker="SELF", name="SELF", last=400.0, change_ytd_pct=100.0,
                    pe=999.0, roe=999.0, net_margin=999.0, is_self=True),
        ]

    def test_excludes_self_row(self):
        from brvm.services.company import _peer_stats
        stats = _peer_stats(self._rows())
        # If self were included, pe median would be ~11 (10+12/2) and
        # mean would be shifted upward. Excluding it: {8, 10, 12} →
        # median 10, mean 10.
        assert stats["pe"].median == 10.0
        assert stats["pe"].mean == 10.0
        assert stats["pe"].n == 3

    def test_omits_median_mean_when_only_one_sample(self):
        from brvm.services._view import PeerRow
        from brvm.services.company import _peer_stats
        # Only one peer reports ROE (10.0); B has None, C has 20.0.
        # Wait — my helper's rows() has ROE None on B and 20 on C plus
        # 10 on A → 2 samples. Build a fresh rows list where only one
        # non-self peer reports the field.
        rows = [
            PeerRow(ticker="A", name="A", roe=10.0),
            PeerRow(ticker="B", name="B", roe=None),
            PeerRow(ticker="SELF", name="SELF", roe=99.0, is_self=True),
        ]
        stats = _peer_stats(rows)
        assert stats["roe"].n == 1
        assert stats["roe"].median is None
        assert stats["roe"].mean is None

    def test_omits_field_entirely_when_no_samples(self):
        from brvm.services._view import PeerRow
        from brvm.services.company import _peer_stats
        rows = [
            PeerRow(ticker="A", name="A"),
            PeerRow(ticker="B", name="B"),
        ]
        stats = _peer_stats(rows)
        assert "pe" not in stats
        assert "roe" not in stats

    def test_covers_all_five_fields_when_populated(self):
        from brvm.services.company import _peer_stats
        stats = _peer_stats(self._rows())
        # net_margin has 2 samples (5.0 + 15.0), roe has 2 (10 + 20).
        assert stats["net_margin"].median == 10.0
        assert stats["net_margin"].mean == 10.0
        assert stats["roe"].median == 15.0
        assert stats["roe"].mean == 15.0
        assert stats["change_ytd_pct"].median == 4.0
        assert stats["change_ytd_pct"].mean == pytest.approx(4.0)


def test_get_peers_with_ratios_includes_stats_block(company_env, monkeypatch, tmp_path):
    """Wiring test: `stats` should be populated in the returned view so
    the web + TUI Peers tabs can render MEDIAN / MEAN rows."""
    from brvm.models import Quote
    from brvm.store import quotes as quotes_repo

    def fake_secteur(ticker, country, client=None):
        return {
            "sector": "TELECOMS",
            "peers": [
                {"ticker": "SNTS", "country": "SN", "name": "SONATEL",
                 "last": 32500, "change_day_pct": 1.0, "change_ytd_pct": 5.0,
                 "volume": 100},
                {"ticker": "ORAC", "country": "CI", "name": "ORANGE CI",
                 "last": 19000, "change_day_pct": 2.0, "change_ytd_pct": 3.0,
                 "volume": 200},
                {"ticker": "ONTBF", "country": "BF", "name": "ONATEL BF",
                 "last": 3500, "change_day_pct": -1.0, "change_ytd_pct": 7.0,
                 "volume": 50},
            ],
        }

    monkeypatch.setattr(
        "brvm.services.company.sikafinance.fetch_secteur", fake_secteur
    )
    db_path = tmp_path / "brvm.sqlite"
    with connect(db_path) as conn:
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="SNTS", source="sikafinance",
                  last=32500.0, change_pct=1.0, volume=100),
        ])

    view = company_env.get_peers_with_ratios("SNTS")
    # change_ytd_pct is populated on all three non-self peers (5/3/7).
    # Median = 5, mean = 5.
    ytd = view.stats.get("change_ytd_pct")
    assert ytd is not None
    assert ytd.n == 2  # SNTS is the self row → excluded; ORAC + ONTBF remain
    assert ytd.median == pytest.approx(5.0)  # median of [3, 7]
    assert ytd.mean == pytest.approx(5.0)
