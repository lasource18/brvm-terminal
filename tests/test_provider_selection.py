import pytest

from brvm.config import reset_settings_cache
from brvm.services import providers


def _reset(monkeypatch, *, base: str = "", key: str = "") -> None:
    monkeypatch.setenv("BRVM_API_BASE", base)
    monkeypatch.setenv("BRVM_API_KEY", key)
    reset_settings_cache()


def test_scrape_when_no_api_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _reset(monkeypatch)
    p = providers.select_provider()
    assert p.name == "scrape"


def test_api_credentials_return_fallback_composite(monkeypatch, tmp_path):
    """F-13: filling BRVM_API_* must no longer strand ingest on the
    stub. `select_provider` returns a compound provider that tries API
    first and falls back to the scraper."""
    monkeypatch.chdir(tmp_path)
    _reset(monkeypatch, base="https://example.test", key="xxx")
    p = providers.select_provider()
    assert p.name == "api+scrape"


def test_fallback_delegates_on_not_implemented(monkeypatch, tmp_path):
    """FallbackProvider swallows NotImplementedError from the primary
    (the current ApiProvider stub) and returns whatever the fallback
    yields — no exception surfaces to the caller."""
    monkeypatch.chdir(tmp_path)
    _reset(monkeypatch, base="https://example.test", key="xxx")

    class _StubScrape:
        name = "scrape"

        def refresh_securities(self):
            return (["security"], ["quote"], ["index"])

        def history(self, ticker, country):
            return ["bar"]

    stub = _StubScrape()
    fp = providers.FallbackProvider(
        primary=providers.ApiProvider("https://x", "y"),
        fallback=stub,
    )
    assert fp.refresh_securities() == (["security"], ["quote"], ["index"])
    assert fp.history("SNTS", "sn") == ["bar"]


def test_fallback_reraises_real_transport_errors(monkeypatch, tmp_path):
    """A NotImplementedError means "stub" — we recover. Any other
    exception (network timeout, malformed JSON) is a genuine API
    failure and must propagate so the caller can log / retry, not be
    swallowed into silent scrape drift."""
    monkeypatch.chdir(tmp_path)
    _reset(monkeypatch, base="https://example.test", key="xxx")

    class _BustedApi:
        name = "api"

        def refresh_securities(self):
            raise TimeoutError("upstream down")

        def history(self, ticker, country):
            raise TimeoutError("upstream down")

    class _NeverCalled:
        name = "scrape"

        def refresh_securities(self):
            raise AssertionError("scrape must not be invoked")

        def history(self, ticker, country):
            raise AssertionError("scrape must not be invoked")

    fp = providers.FallbackProvider(primary=_BustedApi(), fallback=_NeverCalled())
    with pytest.raises(TimeoutError):
        fp.refresh_securities()
    with pytest.raises(TimeoutError):
        fp.history("SNTS", "sn")


@pytest.fixture(autouse=True)
def _restore_settings():
    yield
    reset_settings_cache()
