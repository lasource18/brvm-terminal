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


def test_api_stub_when_credentials_present(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _reset(monkeypatch, base="https://example.test", key="xxx")
    p = providers.select_provider()
    assert p.name == "api"
    with pytest.raises(NotImplementedError):
        p.refresh_securities()


@pytest.fixture(autouse=True)
def _restore_settings():
    yield
    reset_settings_cache()
