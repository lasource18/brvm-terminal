import importlib

import pytest

import brvm.config as cfg
import brvm.services.providers as providers


def _reload_with(monkeypatch, *, base: str = "", key: str = "") -> None:
    monkeypatch.setenv("BRVM_API_BASE", base)
    monkeypatch.setenv("BRVM_API_KEY", key)
    importlib.reload(cfg)
    importlib.reload(providers)


def test_scrape_when_no_api_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _reload_with(monkeypatch)
    p = providers.select_provider()
    assert p.name == "scrape"


def test_api_stub_when_credentials_present(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _reload_with(monkeypatch, base="https://example.test", key="xxx")
    p = providers.select_provider()
    assert p.name == "api"
    with pytest.raises(NotImplementedError):
        p.refresh_securities()


@pytest.fixture(autouse=True)
def _restore_module_state():
    yield
    # Ensure other tests see the default (no-API) provider config.
    importlib.reload(cfg)
    importlib.reload(providers)
