from kodji.config import Settings


def test_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # avoid picking up repo-root .env
    s = Settings()
    assert s.app_env == "dev"
    assert s.db_path.endswith("kodji.sqlite")
    assert s.http_timeout_s == 15.0
    assert not s.has_api_provider


def test_api_provider_flag(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BRVM_API_BASE", "https://example.test")
    monkeypatch.setenv("BRVM_API_KEY", "secret")
    s = Settings()
    assert s.has_api_provider


def test_llm_defaults_match_the_charter(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    s = Settings()
    assert s.anthropic_model == "claude-haiku-4-5-20251001"
    assert s.llm_daily_cap_cents == 100  # hard $1/day
    assert not s.has_llm


def test_llm_flag_and_cap_override(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_DAILY_CAP_CENTS", "25")
    s = Settings()
    assert s.has_llm
    assert s.llm_daily_cap_cents == 25
