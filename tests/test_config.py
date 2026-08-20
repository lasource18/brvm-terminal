from brvm.config import Settings


def test_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # avoid picking up repo-root .env
    s = Settings()
    assert s.app_env == "dev"
    assert s.db_path.endswith("brvm.sqlite")
    assert s.http_timeout_s == 15.0
    assert not s.has_api_provider


def test_api_provider_flag(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRVM_API_BASE", "https://example.test")
    monkeypatch.setenv("BRVM_API_KEY", "secret")
    s = Settings()
    assert s.has_api_provider
