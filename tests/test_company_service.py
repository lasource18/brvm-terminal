import pytest

from brvm.db import connect, ensure_migrations_table
from brvm.models import Security
from brvm.store import securities as sec_repo


@pytest.fixture
def company_env(monkeypatch, tmp_path):
    import importlib
    from pathlib import Path

    import brvm.config as cfg

    db_path = tmp_path / "brvm.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    importlib.reload(cfg)
    import brvm.services.company as company_mod

    importlib.reload(company_mod)

    root = Path(__file__).resolve().parents[1]
    with connect(db_path) as conn:
        ensure_migrations_table(conn)
        conn.executescript((root / "migrations" / "0001_init.sql").read_text())
        conn.executescript((root / "migrations" / "0002_watchlists.sql").read_text())
        conn.commit()
        sec_repo.upsert(
            conn, [Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN")]
        )
    company_mod.clear_cache()
    yield company_mod
    importlib.reload(cfg)
    importlib.reload(company_mod)


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
