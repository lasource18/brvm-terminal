"""Post-close daily brief (Phase 6b).

Gathers today's market context (indices + movers + high-relevance
tagged news + upcoming corporate actions), asks Haiku to synthesize a
short markdown brief, and persists one row per UTC day. Re-running for
the same day overwrites — the store owns that invariant via
`INSERT OR REPLACE`.

Design notes
------------
* **Reuses `services/llm.py`** for the client, pricing, and usage
  extraction. Nothing about the brief's plain-text output changes the
  token/cost accounting shape, so pushing to `brief_spend` (a separate
  daily counter added in migration 0010) keeps the budget line clean of
  Phase 3b's tagging spend.
* **No structured output.** The brief is prose the user will read, not
  JSON to be parsed downstream. `output_config` is omitted so the model
  returns free-form markdown — the response text is stored as-is.
* **Budget gate is one-shot.** One call per day; we check the cap before
  the call and record the real cost immediately after so a crash mid-
  pass can't lose spend accounting. If the day's cap is already spent,
  we no-op with a warning instead of falling back on a cheaper prompt.
* **Gathers via existing services.** Market overview + tagged news +
  upcoming actions all come through the same read paths the UI uses, so
  the brief cannot diverge from what a human viewer sees at ~15:00.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from brvm.clock import utc_iso, utcnow
from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.models import Brief
from brvm.services import llm as llm_svc
from brvm.services import market
from brvm.services import news as news_svc
from brvm.services import translation as translation_svc
from brvm.store import briefs as briefs_repo
from brvm.store import spend as spend_repo

log = get(__name__)


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass
class BriefResult:
    """Outcome of one `generate_for()` pass."""

    day: str
    brief: Brief | None = None
    usage: llm_svc.Usage | None = None
    dry_run: bool = False
    llm_disabled: bool = False
    budget_exhausted: bool = False
    failed: bool = False
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        u = self.usage or llm_svc.Usage()
        d: dict[str, Any] = {
            "day": self.day,
            "generated": self.brief is not None,
            "input_tokens": u.input_tokens + u.cache_read_tokens + u.cache_write_tokens,
            "output_tokens": u.output_tokens,
            "usd_micros": u.usd_micros,
        }
        for flag, name in (
            (self.dry_run, "dry_run"),
            (self.llm_disabled, "llm_disabled"),
            (self.budget_exhausted, "budget_exhausted"),
            (self.failed, "failed"),
        ):
            if flag:
                d[name] = 1
        if self.reason:
            d["reason"] = self.reason
        return d


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------


def _row_to_lite(row) -> dict[str, Any]:
    return {
        "ticker": row.ticker,
        "name": row.name,
        "last": row.last,
        "change_pct": row.change_pct,
        "volume": row.volume,
        "turnover": row.turnover,
    }


def _index_to_lite(tile) -> dict[str, Any]:
    return {
        "ticker": tile.ticker,
        "name": tile.name,
        "level": tile.level,
        "change_pct": tile.change_pct,
        "session_date": tile.session_date.isoformat() if tile.session_date else None,
    }


def _news_to_lite(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "tickers": row.tickers,
        "relevance": row.relevance,
        "category": row.category,
        "title": row.title,
        "summary_en": row.summary_en,
        "summary_fr": row.summary_fr,
        "published_at": row.published_at,
        "source": row.source,
    }


def _action_to_lite(row) -> dict[str, Any]:
    return {
        "ticker": row.ticker,
        "name": row.name,
        "kind": row.kind,
        "ex_date": row.ex_date.isoformat() if row.ex_date else None,
        "pay_date": row.pay_date.isoformat() if row.pay_date else None,
        "amount": row.amount,
        "currency": row.currency,
        "yield_pct": row.yield_pct,
    }


def gather_context(
    day: date | None = None,
    *,
    min_relevance: int | None = None,
    max_news_items: int | None = None,
) -> dict[str, Any]:
    """Assemble the structured snapshot the brief writer will read.

    Uses the same read paths as the /overview and /news pages so the
    brief cannot present numbers the UI wouldn't. Returns a dict that
    round-trips through `json.dumps` — this is what lands in
    `briefs.context_json` for reproducibility.
    """
    day = day or utcnow().date()
    min_rel = min_relevance if min_relevance is not None else settings.brief_min_relevance
    max_items = max_news_items or settings.brief_max_news_items

    overview = market.overview(limit=10)
    # A day's news feed = anything tagged with relevance ≥ floor whose
    # published_at falls on `day`. We pull one page big enough to hit
    # the cap, ordered by (published_at DESC) via the store's default.
    feed = news_svc.list_feed(
        date_from=day.isoformat(),
        date_to=day.isoformat(),
        min_relevance=min_rel,
        limit=max_items,
    )
    upcoming = news_svc.list_upcoming_actions(days=7, today=day)

    return {
        "day": day.isoformat(),
        "market_open_at_gather": overview.market_open,
        "generated_utc": overview.generated_utc,
        "indices": [_index_to_lite(t) for t in overview.indices],
        "gainers": [_row_to_lite(r) for r in overview.gainers],
        "losers": [_row_to_lite(r) for r in overview.losers],
        "turnover_leaders": [_row_to_lite(r) for r in overview.turnover_leaders],
        "news": [_news_to_lite(r) for r in feed.items],
        "upcoming_actions": [_action_to_lite(r) for r in upcoming],
    }


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """\
You write the daily post-close brief for the BRVM (Bourse Régionale des \
Valeurs Mobilières), the regional stock exchange of the 8 WAEMU countries, \
based in Abidjan. Prices are in XOF (CFA franc). Source material is in \
French; your brief is in English.

You will receive a JSON snapshot of the trading day (indices, top movers, \
turnover leaders, tagged news, upcoming corporate actions). Produce a \
short markdown brief that a portfolio manager can skim in under a minute.

Structure (in this order, using the exact markdown headings):

# Session recap
2-3 sentences on the session's tone: indices, breadth (gainers vs losers), \
turnover concentration.

# Movers
Bullet list of the 3-6 most notable movers with change %, last price, and \
a one-line reason drawn from the news snapshot when available. Skip \
tickers that only moved on thin volume unless the move is very large.

# News that matters
Group high-relevance news thematically (earnings / dividend / governance / \
capital action / macro). Under each theme, list the specific items with \
ticker + one-line English summary. Skip themes with no items.

# Watch tomorrow
Bullet list of upcoming corporate actions in the next 7 days (ex-date, \
ticker, kind, amount when applicable).

Rules:
- Use only facts present in the JSON. Never invent figures, dates, or \
company activities. If a mover has no matching news item, say so ("no \
news attached") rather than guessing a driver.
- Keep the whole brief under 500 words.
- Do not include a preamble or a sign-off. Start with the "# Session \
recap" heading.
- Currency labels: prices in XOF, percentages with one decimal.\
"""


def build_user_payload(context: dict[str, Any]) -> str:
    return "SNAPSHOT:\n" + json.dumps(context, ensure_ascii=False, indent=None)


def _title_from_markdown(md: str) -> str | None:
    """Best-effort headline for the archive list. Falls back to first
    non-heading line trimmed to ~80 chars."""
    for raw in md.splitlines():
        line = raw.strip()
        if not line:
            continue
        title = line.lstrip("# ").strip() if line.startswith("# ") else line
        return title[:80] if len(title) > 80 else title
    return None


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


def _db_path() -> Path:
    return Path(settings.db_path)


def _call_model(
    context: dict[str, Any],
    *,
    client: Any | None = None,
    model: str | None = None,
    max_output_tokens: int | None = None,
) -> tuple[str, llm_svc.Usage, str]:
    """One Haiku call. Returns (markdown, usage, model_id).

    Raises `LLMResponseError` (carrying the usage billed) on an empty
    reply, and `LLMUnavailable` when no API key is configured.
    """
    client = client or llm_svc.get_client()
    model = model or settings.brief_model
    max_tokens = max_output_tokens or settings.brief_max_output_tokens

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": build_user_payload(context)}],
    )
    usage = llm_svc.usage_from_response(response, model)
    text = llm_svc.response_text(response).strip()

    if not text:
        raise llm_svc.LLMResponseError("empty reply from model", usage=usage)
    return text, usage, model


def generate_for(
    day: date | None = None,
    *,
    client: Any | None = None,
    dry_run: bool = False,
) -> BriefResult:
    """Generate + persist the brief for `day` (default: today UTC).

    Safe to call unconditionally: returns a `BriefResult` explaining what
    happened for every operational case (no key, budget exhausted, empty
    reply, http error, dry run).
    """
    day = day or utcnow().date()
    day_iso = day.isoformat()
    # F-19: spend accounting keys on the real UTC day the API call
    # happens, NOT the covered day. Backfilling N historical briefs
    # in one real day used to get N fresh caps under the old
    # covered-day keying (`day` below) — real-day spend was
    # unbounded by `BRIEF_DAILY_CAP_CENTS`. Analyst notes already
    # bill real today; brings the two in line.
    spend_day = utcnow().date()
    result = BriefResult(day=day_iso)

    context = gather_context(day)

    if dry_run:
        # Report what would be sent, spend nothing. The context stays
        # in-memory only.
        result.dry_run = True
        log.info(
            "brief dry-run for %s: %d news, %d movers, %d upcoming actions",
            day_iso, len(context["news"]),
            len(context["gainers"]) + len(context["losers"]),
            len(context["upcoming_actions"]),
        )
        return result

    if client is None and not settings.has_llm:
        result.llm_disabled = True
        log.warning("brief for %s: no ANTHROPIC_API_KEY, skipping", day_iso)
        return result

    # Budget pre-check. Read what's already been billed today (against
    # `brief_spend` — separate from the tagger / extractor counters).
    with connect(_db_path()) as conn:
        remaining = spend_repo.remaining_micros(
            conn, settings.brief_daily_cap_cents, spend_day, table="brief_spend"
        )
    if remaining <= 0:
        result.budget_exhausted = True
        log.warning("brief for %s: daily cap already spent", day_iso)
        return result

    try:
        markdown, usage, model_id = _call_model(context, client=client)
    except llm_svc.LLMResponseError as e:
        # Bill the usage that was already spent, then bail. A failed
        # brief mustn't hide its cost from the daily counter.
        result.usage = e.usage
        result.failed = True
        result.reason = str(e)
        with connect(_db_path()) as conn:
            spend_repo.add_usage(
                conn,
                input_tokens=e.usage.input_tokens
                + e.usage.cache_read_tokens
                + e.usage.cache_write_tokens,
                output_tokens=e.usage.output_tokens,
                usd_micros=e.usage.usd_micros,
                day=spend_day,
                table="brief_spend",
            )
        log.warning("brief for %s failed: %s", day_iso, e)
        return result
    except Exception as e:  # transport / SDK error — no billing
        result.failed = True
        result.reason = f"transport_error: {e}"
        log.warning("brief for %s failed (no billing): %s", day_iso, e)
        return result

    # PR-I: translate the freshly-generated EN markdown into FR. Runs
    # inside the same generation pass so the two versions land together
    # in one DB write. A failure here (transport error, empty reply,
    # budget cap crossed by the primary call) is *soft*: the brief still
    # persists in EN, `markdown_fr` stays NULL, and the /brief route
    # falls back with a "translation pending" badge. Next brief re-run
    # gets another shot at translation.
    markdown_fr: str | None = None
    translation_generated_utc: str | None = None
    translation_usage = _translate_or_none(markdown, client=client, day=spend_day)
    if translation_usage is not None:
        markdown_fr = translation_usage[0]
        translation_generated_utc = utc_iso()

    brief = Brief(
        day=day_iso,
        model=model_id,
        title=_title_from_markdown(markdown),
        markdown=markdown,
        markdown_fr=markdown_fr,
        translation_generated_utc=translation_generated_utc,
        context_json=json.dumps(context, ensure_ascii=False),
        input_tokens=usage.input_tokens
        + usage.cache_read_tokens
        + usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        usd_micros=usage.usd_micros,
        generated_utc=utc_iso(),
        session_date=day_iso,
    )
    with connect(_db_path()) as conn:
        briefs_repo.upsert(conn, brief)
        spend_repo.add_usage(
            conn,
            input_tokens=brief.input_tokens,
            output_tokens=brief.output_tokens,
            usd_micros=brief.usd_micros,
            day=spend_day,
            table="brief_spend",
        )
        # Translation billing rides on the same daily counter — it's
        # conceptually the same product (the day's brief) and the cap
        # was sized with translation in mind.
        if translation_usage is not None:
            _, t_usage = translation_usage
            spend_repo.add_usage(
                conn,
                input_tokens=t_usage.input_tokens
                + t_usage.cache_read_tokens
                + t_usage.cache_write_tokens,
                output_tokens=t_usage.output_tokens,
                usd_micros=t_usage.usd_micros,
                day=spend_day,
                table="brief_spend",
            )
    result.brief = brief
    result.usage = usage
    log.info(
        "brief for %s: %d in / %d out ($%.4f) via %s%s",
        day_iso, brief.input_tokens, brief.output_tokens,
        brief.usd_micros / 1_000_000, model_id,
        " · fr translated" if markdown_fr else " · fr pending",
    )
    return result


def _translate_or_none(
    source_markdown: str,
    *,
    client: Any | None,
    day: date,
) -> tuple[str, llm_svc.Usage] | None:
    """Attempt an EN → FR translation, returning (text, usage) on success
    or None on any soft failure (no key, empty reply, transport error).

    Called from `generate_for` inside the same DB transaction as the
    primary brief write so both versions land atomically. Kept as a
    module-level helper (not a nested function) so tests can monkey-patch
    it to skip the second LLM call.
    """
    if client is None and not translation_svc.has_llm():
        return None
    try:
        result = translation_svc.translate_markdown_to_fr(
            source_markdown, client=client
        )
    except llm_svc.LLMResponseError as e:
        # Translation billed but returned nothing usable — still record
        # the spend against the daily counter so the cap stays honest.
        log.warning("brief translation failed (empty reply): %s", e)
        with connect(_db_path()) as conn:
            spend_repo.add_usage(
                conn,
                input_tokens=e.usage.input_tokens
                + e.usage.cache_read_tokens
                + e.usage.cache_write_tokens,
                output_tokens=e.usage.output_tokens,
                usd_micros=e.usage.usd_micros,
                day=day,
                table="brief_spend",
            )
        return None
    except Exception as e:  # transport / SDK error — no billing
        log.warning("brief translation failed (no billing): %s", e)
        return None
    return result.text, result.usage


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def get_brief(day: str | date) -> Brief | None:
    d = day if isinstance(day, str) else day.isoformat()
    with connect(_db_path()) as conn:
        return briefs_repo.get(conn, d)


def latest_brief() -> Brief | None:
    with connect(_db_path()) as conn:
        return briefs_repo.latest(conn)


def list_recent_briefs(*, limit: int = 30) -> list[Brief]:
    with connect(_db_path()) as conn:
        return briefs_repo.list_recent(conn, limit=limit)


def spent_today_micros(day: date | None = None) -> int:
    day = day or utcnow().date()
    with connect(_db_path()) as conn:
        return spend_repo.spent_micros(conn, day, table="brief_spend")


__all__ = [
    "BriefResult",
    "gather_context",
    "generate_for",
    "get_brief",
    "latest_brief",
    "list_recent_briefs",
    "spent_today_micros",
]
