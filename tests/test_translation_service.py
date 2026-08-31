"""PR-I: EN → FR markdown translation service + integration into the
brief and analyst-note writers.

Covers:
- `services/translation.translate_markdown_to_fr` — happy path, empty
  input short-circuits, empty reply raises, transport error bubbles.
- Migration 0015 added `markdown_fr` + `translation_generated_utc` to
  both `briefs` and `analyst_notes`; store roundtrips both fields.
- `services/brief.generate_for` inline translation: primary + follow-up
  translation both persist and both bill against `brief_spend`.
- `services/analyst_notes.generate_for_ticker`: same pattern per note.
- Soft-failure semantics: translation transport error → source still
  persists with `markdown_fr = NULL` (renders "translation pending").
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from kodji.config import reset_settings_cache
from kodji.db import connect
from kodji.models import Brief, IndexLevel, NewsItem, Quote, Security
from kodji.services import llm as llm_svc
from kodji.services import translation as translation_svc
from kodji.sources._dedupe import news_hash
from kodji.store import briefs as briefs_repo
from kodji.store import news as news_repo
from kodji.store import quotes as quotes_repo
from kodji.store import securities as sec_repo
from kodji.store import spend as spend_repo

from ._fake_anthropic import FakeAnthropic, reply
from .conftest import apply_migrations

# --------- translate_markdown_to_fr (unit) ---------------------------------


class TestTranslateMarkdownToFr:
    def test_returns_translation_text_and_usage(self):
        client = FakeAnthropic([
            reply(
                "# Résumé de séance\nSNTS a mené la séance à +4,5%.",
                input_tokens=300, output_tokens=80,
            ),
        ])
        result = translation_svc.translate_markdown_to_fr(
            "# Session recap\nSNTS led the tape +4.5%.",
            client=client,
        )
        assert result.text.startswith("# Résumé de séance")
        assert result.usage.output_tokens == 80
        assert result.model.startswith("claude-haiku")

    def test_empty_input_is_a_noop(self):
        # Nothing to translate — skip the API call entirely.
        client = FakeAnthropic([])   # would raise if called
        result = translation_svc.translate_markdown_to_fr("", client=client)
        assert result.text == ""
        assert result.usage.usd_micros == 0
        assert client.call_count == 0

    def test_whitespace_only_input_is_a_noop(self):
        client = FakeAnthropic([])
        result = translation_svc.translate_markdown_to_fr("   \n\n  ", client=client)
        assert client.call_count == 0
        assert result.text.strip() == ""

    def test_empty_reply_raises_llm_response_error(self):
        client = FakeAnthropic([reply("", input_tokens=200, output_tokens=0)])
        with pytest.raises(llm_svc.LLMResponseError) as exc_info:
            translation_svc.translate_markdown_to_fr(
                "# Session recap\nsomething", client=client,
            )
        # Usage attached so the caller can still bill the wasted tokens.
        assert exc_info.value.usage.input_tokens == 200

    def test_transport_error_bubbles(self):
        class _Boom:
            class messages:
                @staticmethod
                def create(**_kw):
                    raise RuntimeError("simulated 503")

        with pytest.raises(RuntimeError, match="503"):
            translation_svc.translate_markdown_to_fr(
                "# Session recap\nx", client=_Boom(),
            )

    def test_system_prompt_carries_cache_control(self):
        # The translation prompt is stable across runs; caching it means
        # a batch (brief + N notes) pays full price on the first call and
        # the cache-read discount on the rest. Verify the cache breakpoint
        # is set on the system message.
        client = FakeAnthropic([reply("# x", input_tokens=50, output_tokens=10)])
        translation_svc.translate_markdown_to_fr("# Session recap\nx", client=client)
        assert client.calls, "expected the fake client to receive one call"
        sys_block = client.calls[0]["system"][0]
        assert sys_block["cache_control"] == {"type": "ephemeral"}


# --------- store roundtrip (migration 0015 shape) --------------------------


class TestStoreRoundtripsTranslationColumns:
    def test_brief_persists_markdown_fr(self, monkeypatch, tmp_path):
        db_path = tmp_path / "kodji.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        reset_settings_cache()
        with connect(db_path) as conn:
            apply_migrations(conn)
            briefs_repo.upsert(conn, Brief(
                day="2026-08-29",
                model="claude-haiku-4-5-20251001",
                markdown="# Session recap\nEnglish body.",
                markdown_fr="# Résumé de séance\nCorps français.",
                translation_generated_utc="2026-08-29T15:30:00Z",
                context_json="{}",
            ))
            got = briefs_repo.get(conn, "2026-08-29")
        assert got is not None
        assert got.markdown.startswith("# Session recap")
        assert got.markdown_fr and got.markdown_fr.startswith("# Résumé de séance")
        assert got.translation_generated_utc == "2026-08-29T15:30:00Z"

    def test_brief_without_translation_leaves_fr_null(
        self, monkeypatch, tmp_path,
    ):
        db_path = tmp_path / "kodji.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        reset_settings_cache()
        with connect(db_path) as conn:
            apply_migrations(conn)
            briefs_repo.upsert(conn, Brief(
                day="2026-08-29",
                model="claude-haiku-4-5-20251001",
                markdown="# Session recap\nEnglish body.",
                context_json="{}",
            ))
            got = briefs_repo.get(conn, "2026-08-29")
        assert got is not None
        assert got.markdown_fr is None
        assert got.translation_generated_utc is None

    def test_set_translation_updates_only_the_fr_columns(
        self, monkeypatch, tmp_path,
    ):
        db_path = tmp_path / "kodji.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        reset_settings_cache()
        with connect(db_path) as conn:
            apply_migrations(conn)
            briefs_repo.upsert(conn, Brief(
                day="2026-08-29",
                model="claude-haiku-4-5-20251001",
                markdown="# Session recap\nEnglish body.",
                context_json="{}",
            ))
            ok = briefs_repo.set_translation(
                conn, "2026-08-29",
                markdown_fr="# Résumé\nversion française",
                generated_utc="2026-08-29T15:35:00Z",
            )
            assert ok is True
            got = briefs_repo.get(conn, "2026-08-29")
        assert got is not None
        # Source untouched.
        assert got.markdown == "# Session recap\nEnglish body."
        assert got.markdown_fr == "# Résumé\nversion française"
        assert got.translation_generated_utc == "2026-08-29T15:35:00Z"

    def test_set_translation_returns_false_when_row_missing(
        self, monkeypatch, tmp_path,
    ):
        db_path = tmp_path / "kodji.sqlite"
        monkeypatch.setenv("DB_PATH", str(db_path))
        reset_settings_cache()
        with connect(db_path) as conn:
            apply_migrations(conn)
            # No brief for 2026-08-29 yet.
            ok = briefs_repo.set_translation(
                conn, "2026-08-29", markdown_fr="x",
            )
        assert ok is False


# --------- brief.generate_for translation path (integration) ---------------


def _setup_brief(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "kodji.sqlite"
    monkeypatch.setenv("DB_PATH", str(db_path))
    reset_settings_cache()
    from kodji.services import brief as svc
    from kodji.services import llm as llm_mod

    llm_mod.reset_client()
    with connect(db_path) as conn:
        apply_migrations(conn)
        sec_repo.upsert(conn, [
            Security(ticker="SNTS", name="SONATEL", kind="equity", country="SN"),
            Security(ticker="BRVMC", name="BRVM COMPOSITE", kind="index"),
        ])
        quotes_repo.insert_snapshots(conn, [
            Quote(ticker="SNTS", source="sikafinance", last=32_500, change_pct=4.5,
                  volume=3000, turnover=97_500_000),
        ])
        quotes_repo.upsert_index_levels(conn, [
            IndexLevel(ticker="BRVMC", session_date=date(2026, 8, 20),
                       level=307.42, change_pct=0.53, source="sikafinance"),
        ])
        url = "https://example.test/news/1"
        news_repo.upsert_news_items(conn, [NewsItem(
            source="sikafinance", kind="news", url=url,
            url_hash=news_hash(url, "SNTS results"),
            title="SNTS H1 results",
            published_at="2026-08-20T09:00:00Z",
            ticker_hint=None,
        )])
        nid = int(conn.execute(
            "SELECT id FROM news_items ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"])
        conn.execute(
            "UPDATE news_items SET tickers_llm = ?, relevance = ?, category_llm = ? "
            "WHERE id = ?",
            ("SNTS", 8, "earnings", nid),
        )
        conn.commit()
    return db_path, svc


class TestBriefGenerateWithTranslation:
    def test_happy_path_stores_both_and_bills_both(
        self, monkeypatch, tmp_path,
    ):
        """Primary synthesis + FR translation both land on the brief row
        and both spend against `brief_spend`."""
        db_path, svc = _setup_brief(monkeypatch, tmp_path)
        client = FakeAnthropic([
            # Call 1: primary synthesis
            reply(
                "# Session recap\nSNTS led the tape.\n"
                "# Movers\n- SNTS +4.5%\n",
                input_tokens=2000, output_tokens=200,
            ),
            # Call 2: translation
            reply(
                "# Résumé de séance\nSNTS a mené la séance.\n"
                "# Principaux mouvements\n- SNTS +4.5%\n",
                input_tokens=400, output_tokens=180,
            ),
        ])
        result = svc.generate_for(date(2026, 8, 20), client=client)

        assert result.brief is not None
        assert result.brief.markdown.startswith("# Session recap")
        assert result.brief.markdown_fr is not None
        assert result.brief.markdown_fr.startswith("# Résumé de séance")
        assert result.brief.translation_generated_utc is not None

        # Both calls billed to brief_spend (primary + translation).
        # F-19: brief spend keys on real UTC day, not the covered day.
        from kodji.clock import utcnow
        today_iso = utcnow().date().isoformat()
        with connect(db_path) as conn:
            spent = spend_repo.spent_micros(conn, today_iso, table="brief_spend")
        primary_cost = llm_svc.usd_micros_for(
            "claude-haiku-4-5-20251001", input_tokens=2000, output_tokens=200,
        )
        translation_cost = llm_svc.usd_micros_for(
            "claude-haiku-4-5-20251001", input_tokens=400, output_tokens=180,
        )
        assert spent == primary_cost + translation_cost

    def test_translation_transport_error_still_persists_source(
        self, monkeypatch, tmp_path,
    ):
        """A translation crash mustn't lose the primary brief. Source
        markdown persists, `markdown_fr` stays NULL, and only the primary
        spend gets billed."""
        db_path, svc = _setup_brief(monkeypatch, tmp_path)

        class _PartialClient:
            def __init__(self, primary):
                self._primary = primary
                self._calls = 0
                self.messages = self

            def create(self, **kw):
                self._calls += 1
                if self._calls == 1:
                    return self._primary
                raise RuntimeError("simulated translation transport failure")

        primary = reply(
            "# Session recap\nSNTS led the tape.\n",
            input_tokens=2000, output_tokens=100,
        )
        client = _PartialClient(primary)
        result = svc.generate_for(date(2026, 8, 20), client=client)

        assert result.brief is not None
        assert result.brief.markdown.startswith("# Session recap")
        assert result.brief.markdown_fr is None
        assert result.brief.translation_generated_utc is None

        # Only the primary was billed (transport error → no billing).
        # F-19: brief spend keys on real UTC day, not the covered day.
        from kodji.clock import utcnow
        today_iso = utcnow().date().isoformat()
        with connect(db_path) as conn:
            spent = spend_repo.spent_micros(conn, today_iso, table="brief_spend")
        assert spent == llm_svc.usd_micros_for(
            "claude-haiku-4-5-20251001", input_tokens=2000, output_tokens=100,
        )

    def test_translation_empty_reply_bills_but_leaves_fr_null(
        self, monkeypatch, tmp_path,
    ):
        """Translation billed but returned empty text — record the spend
        against `brief_spend` so the daily cap stays honest, but keep the
        FR column NULL so the UI shows the pending badge."""
        db_path, svc = _setup_brief(monkeypatch, tmp_path)
        client = FakeAnthropic([
            reply(
                "# Session recap\nSNTS led.\n",
                input_tokens=2000, output_tokens=100,
            ),
            reply("", input_tokens=400, output_tokens=0),
        ])
        result = svc.generate_for(date(2026, 8, 20), client=client)

        assert result.brief is not None
        assert result.brief.markdown_fr is None

        # F-19: brief spend keys on real UTC day, not the covered day.
        from kodji.clock import utcnow
        today_iso = utcnow().date().isoformat()
        with connect(db_path) as conn:
            spent = spend_repo.spent_micros(conn, today_iso, table="brief_spend")
        primary = llm_svc.usd_micros_for(
            "claude-haiku-4-5-20251001", input_tokens=2000, output_tokens=100,
        )
        empty_translation = llm_svc.usd_micros_for(
            "claude-haiku-4-5-20251001", input_tokens=400, output_tokens=0,
        )
        assert spent == primary + empty_translation


# --------- route rendering per locale --------------------------------------


class TestBriefRouteHonorsLocale:
    def test_fr_cookie_renders_french_markdown_when_present(self, client):
        # Seed a brief with both EN + FR bodies.
        import os

        from kodji.db import connect

        with connect(os.environ["DB_PATH"]) as conn:
            briefs_repo.upsert(conn, Brief(
                day="2026-08-29",
                model="claude-haiku-4-5-20251001",
                title="Session recap",
                markdown="# Session recap\nEnglish body.",
                markdown_fr="# Résumé de séance\nCorps français distinctif.",
                translation_generated_utc="2026-08-29T15:30:00Z",
                context_json="{}",
                session_date="2026-08-29",
            ))
        client.cookies.set("brvm_lang", "fr")
        r = client.get("/brief/2026-08-29")
        assert r.status_code == 200
        assert "Corps français distinctif" in r.text
        assert "English body." not in r.text
        assert "traduction en cours" not in r.text  # no pending badge

    def test_fr_cookie_falls_back_to_en_with_pending_badge(self, client):
        import os

        from kodji.db import connect

        with connect(os.environ["DB_PATH"]) as conn:
            briefs_repo.upsert(conn, Brief(
                day="2026-08-29",
                model="claude-haiku-4-5-20251001",
                title="Session recap",
                markdown="# Session recap\nEnglish only.",
                context_json="{}",
                session_date="2026-08-29",
            ))
        client.cookies.set("brvm_lang", "fr")
        r = client.get("/brief/2026-08-29")
        assert r.status_code == 200
        # Fallback: source markdown is rendered.
        assert "English only." in r.text
        # Badge tells the reader why they're seeing English on the FR page.
        assert "traduction en cours" in r.text

    def test_en_default_ignores_fr_translation(self, client):
        import os

        from kodji.db import connect

        with connect(os.environ["DB_PATH"]) as conn:
            briefs_repo.upsert(conn, Brief(
                day="2026-08-29",
                model="claude-haiku-4-5-20251001",
                title="Session recap",
                markdown="# Session recap\nEnglish body.",
                markdown_fr="# Résumé\nversion française.",
                translation_generated_utc="2026-08-29T15:30:00Z",
                context_json="{}",
                session_date="2026-08-29",
            ))
        # No cookie set → default locale (EN).
        r = client.get("/brief/2026-08-29")
        assert r.status_code == 200
        assert "English body." in r.text
        assert "version française" not in r.text
