"""Weekly per-ticker analyst notes (Phase 6c).

Since the BRVM has essentially no public sell-side coverage, we generate
our own machine-written note per ticker per ISO week — fed the last 30
days of tagged news, latest fundamentals (annual + interim), computed
ratios, and 90 days of price action.

Design notes
------------
* **Weekly cadence, per-ticker iteration.** `generate_for_ticker(ticker)`
  is the atomic unit; `generate_for_all()` walks every active equity with
  a polite pause between calls. Same-week rerun overwrites via
  `INSERT OR REPLACE` in the store — a rerun mid-week produces a fresher
  take on the same week's data, which is what a reader expects.
* **Sonnet, not Haiku.** An analyst note is a much richer write than the
  Phase 6b daily brief and benefits from Sonnet's deeper reasoning. A
  full weekly pass at ~$0.04/ticker x 47 equities ≈ $1.90; the $3/day
  cap gives one full retry of headroom.
* **Budget is per-call.** Before every ticker's call the worker checks
  `note_spend` against `NOTES_DAILY_CAP_CENTS`. Once crossed, remaining
  tickers are skipped with a warning — a partial week is better than
  half-a-week of budget accidentally spent on the same 47th ticker on
  every rerun.
* **`context_json` is stored.** A future re-run with a different prompt
  can regenerate from the captured snapshot without re-hitting the news
  feed, financials, or price stats.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from brvm.clock import utc_iso, utcnow
from brvm.config import settings
from brvm.db import connect
from brvm.logging import get
from brvm.models import AnalystNote
from brvm.services import fundamentals, history, market, ratios

# Reuse the LLM helpers Phase 6b promoted to public names.
from brvm.services import llm as llm_svc
from brvm.services import news as news_svc
from brvm.store import analyst_notes as notes_repo
from brvm.store import securities as sec_repo
from brvm.store import spend as spend_repo

log = get(__name__)


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass
class TickerResult:
    """Outcome of one `generate_for_ticker()` pass."""

    ticker: str
    week_start: str
    note: AnalystNote | None = None
    usage: llm_svc.Usage | None = None
    dry_run: bool = False
    llm_disabled: bool = False
    budget_exhausted: bool = False
    failed: bool = False
    reason: str = ""

    def as_short_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "ticker": self.ticker,
            "week": self.week_start,
            "generated": self.note is not None,
        }
        if self.usage:
            d["usd_micros"] = self.usage.usd_micros
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


@dataclass
class PassCounts:
    """Aggregate outcome of one `generate_for_all()` sweep."""

    week_start: str
    considered: int = 0
    generated: int = 0
    skipped_budget: int = 0
    failed: int = 0
    dry_run_count: int = 0
    tickers_generated: list[str] = field(default_factory=list)
    tickers_failed: list[str] = field(default_factory=list)
    total_usd_micros: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "week": self.week_start,
            "considered": self.considered,
            "generated": self.generated,
            "skipped_budget": self.skipped_budget,
            "failed": self.failed,
            "dry_run": self.dry_run_count,
            "usd_micros": self.total_usd_micros,
        }


# ---------------------------------------------------------------------------
# Week helpers
# ---------------------------------------------------------------------------


def iso_week_monday(d: date | None = None) -> date:
    """Monday of the ISO week containing `d` (default: today UTC).
    Same convention as `datetime.date.isocalendar()` — every day
    Mon-Sun maps to that week's Monday."""
    d = d or utcnow().date()
    return d - timedelta(days=d.weekday())


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------


def _news_lite(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "relevance": row.relevance,
        "category": row.category,
        "title": row.title,
        "summary_en": row.summary_en,
        "summary_fr": row.summary_fr,
        "published_at": row.published_at,
    }


def _financials_lite(fs) -> dict[str, Any] | None:
    if not fs.has_data:
        return None
    # Send the whole 5-year (default limit) table transposed to per-period
    # rows so the model can eyeball trends without doing index math.
    rows: list[dict[str, Any]] = []
    for i, year in enumerate(fs.periods):
        rows.append({
            "period_year": year,
            "period_kind": "annual",
            **{k: fs.metrics[k][i] for k in fs.metrics},
        })
    return {"currency": fs.currency, "rows": rows}


def _interim_lite(interim) -> dict[str, Any] | None:
    if interim is None or not interim.has_data:
        return None
    return {
        "period_year": interim.period_year,
        "period_kind": interim.period_kind,
        "currency": interim.currency,
        "metrics": interim.metrics,
    }


def _ratios_lite(rv) -> dict[str, Any] | None:
    """Pull the numeric value out of each `Ratio` dataclass, ignoring
    provenance strings (the model doesn't need them and they cost
    tokens)."""
    if rv is None:
        return None
    payload: dict[str, Any] = {
        "period_year": rv.period_year,
        "period_kind": rv.period_kind,
        "currency": rv.currency,
        "market_cap_xof": rv.market_cap_xof,
    }
    for name in (
        "pe", "pb", "ps", "dividend_yield", "payout_ratio", "earnings_yield",
        "roe", "roa", "net_margin", "operating_margin",
        "revenue_growth", "net_income_growth", "eps_growth",
        "financial_leverage", "equity_ratio",
    ):
        r = getattr(rv, name, None)
        payload[name] = r.value if r is not None else None
    return payload


def _price_stats(bars: list) -> dict[str, Any] | None:
    """Compressed price action summary from up to 90 daily bars —
    a full series would be wasted tokens for the model."""
    if not bars:
        return None
    # `bars` from history.get_history is newest-first.
    closes = [b.close for b in bars if b.close is not None]
    if not closes:
        return None
    latest = closes[0]
    earliest = closes[-1]
    change_pct = (latest - earliest) / earliest * 100 if earliest else None
    high = max(closes)
    low = min(closes)
    payload: dict[str, Any] = {
        "days": len(closes),
        "latest_close": latest,
        "period_high": high,
        "period_low": low,
        "period_change_pct": change_pct,
    }
    if len(closes) >= 5:
        # Simple realised vol proxy: stdev of daily log returns x sqrt(252).
        import math
        rets = [
            math.log(closes[i] / closes[i + 1])
            for i in range(len(closes) - 1)
            if closes[i + 1]
        ]
        if len(rets) >= 2:
            payload["annualised_vol_pct"] = statistics.pstdev(rets) * math.sqrt(252) * 100
    return payload


def _quote_lite(sec_view) -> dict[str, Any] | None:
    q = sec_view.quote if sec_view else None
    if q is None:
        return None
    return {
        "last": q.last,
        "change_pct": q.change_pct,
        "volume": q.volume,
        "turnover": q.turnover,
        "captured_utc": q.captured_utc,
    }


def _security_meta(sec_view) -> dict[str, Any]:
    return {
        "ticker": sec_view.ticker,
        "name": sec_view.name,
        "country": sec_view.country,
        "kind": sec_view.kind,
    }


def gather_context(
    ticker: str,
    *,
    week_start: date | None = None,
    lookback_days: int | None = None,
    max_news_items: int | None = None,
) -> dict[str, Any] | None:
    """Assemble the structured snapshot the note writer will read.

    Returns None when the ticker isn't a known equity — indices and
    bonds don't get notes (per the charter's per-ticker equity scope).
    The dict round-trips through `json.dumps` and lands in
    `analyst_notes.context_json` for reproducibility.
    """
    ticker = ticker.upper()
    week_start = week_start or iso_week_monday()
    lookback = lookback_days or settings.notes_lookback_days
    max_items = max_news_items or settings.notes_max_news_items

    sec_view = market.get_security(ticker)
    if sec_view is None or sec_view.kind != "equity":
        return None

    since = (week_start - timedelta(days=lookback)).isoformat()
    until = week_start.isoformat()
    feed = news_svc.list_feed(
        ticker=ticker,
        date_from=since,
        date_to=until,
        limit=max_items,
    )
    fs = fundamentals.get_financials_series(ticker)
    interim = fundamentals.get_latest_interim(ticker)
    latest_ratios = ratios.get_latest_ratios(ticker)
    bars = history.get_history(ticker, sec_view.country)
    # Trim to ~90 sessions for the price-stats summary.
    trimmed = bars[:90] if bars else []
    ownership = fundamentals.get_ownership(ticker)
    segments = fundamentals.get_segments(ticker)

    return {
        "ticker": ticker,
        "week_start": week_start.isoformat(),
        "security": _security_meta(sec_view),
        "quote": _quote_lite(sec_view),
        "price_stats": _price_stats(trimmed),
        "financials": _financials_lite(fs),
        "interim": _interim_lite(interim),
        "ratios": _ratios_lite(latest_ratios),
        "ownership": [
            {"holder": h["holder"], "share_pct": h["share_pct"]}
            for h in ownership.holders
        ] if ownership.has_data else [],
        "segments": {
            "business": segments.business,
            "geo": segments.geo,
        } if segments.has_data else {},
        "news": [_news_lite(r) for r in feed.items],
    }


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = """\
You write a weekly analyst-style note on one BRVM-listed company. The \
BRVM (Bourse Régionale des Valeurs Mobilières) is the regional stock \
exchange of the 8 WAEMU countries, based in Abidjan. Prices are in XOF \
(CFA franc). Source material is in French; your note is in English.

You will receive a JSON snapshot: the company's latest quote, 90-day \
price stats, 5-year annual financials + most recent interim, headline \
ratios, ownership, segment split, and the last 30 days of Haiku-tagged \
news. Produce a concise markdown note (~700-1000 words) a portfolio \
manager can read in 3 minutes.

Structure (in this order, using the exact markdown headings):

# Snapshot
2-3 sentences on the company and its recent trading action. Include \
the sector, headline valuation multiple, and the 90-day return.

# Recent developments
Bullet list of the most material items from the news snapshot. Group \
tightly-related items on one line; skip low-relevance filler. If there \
is no news, say "no notable news this window".

# Financial position
3-5 sentences reading the latest annual + interim (when present). Call \
out revenue and profit trend, margin trajectory, balance-sheet shape \
(leverage / equity ratio when useful). Never state a figure that isn't \
in the snapshot.

# Ratios read-across
2-3 sentences on how the ratios compare to a normal range for the \
company's sector (e.g. Sonatel telecom peers, a bank vs sector \
average). Flag anything that stands out (very high P/E, negative \
growth, unusual payout).

# Risks & watch items
Bullet list of forward-looking risks the reader should track: known \
upcoming events (results, dividends, capital actions), open questions \
raised by the news, macro exposures (WAEMU, currency).

Rules:
- Use only facts present in the JSON. Never invent figures, dates, or \
company activities. If a section has no supporting data, say so.
- Keep it under 1000 words.
- Do not include a preamble or a sign-off. Start with the "# Snapshot" \
heading.
- Currency labels: prices and monetary amounts in XOF (or the currency \
noted on the financials row), percentages with one decimal.
- This note is machine-generated. Do not claim insider or expert \
information.\
"""


def build_user_payload(context: dict[str, Any]) -> str:
    return "SNAPSHOT:\n" + json.dumps(context, ensure_ascii=False, indent=None)


def _title_from_markdown(md: str) -> str | None:
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
    client = client or llm_svc.get_client()
    model = model or settings.notes_model
    max_tokens = max_output_tokens or settings.notes_max_output_tokens

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


def generate_for_ticker(
    ticker: str,
    *,
    week_start: date | None = None,
    client: Any | None = None,
    dry_run: bool = False,
) -> TickerResult:
    """Generate + persist a note for one ticker's week. Safe to call
    unconditionally — every operational case (no key, budget exhausted,
    non-equity ticker, empty reply, transport error, dry run) returns a
    `TickerResult` with the appropriate flags set."""
    ticker = ticker.upper()
    week_start = week_start or iso_week_monday()
    week_iso = week_start.isoformat()
    result = TickerResult(ticker=ticker, week_start=week_iso)

    context = gather_context(ticker, week_start=week_start)
    if context is None:
        result.failed = True
        result.reason = "not_equity_or_unknown_ticker"
        return result

    if dry_run:
        result.dry_run = True
        log.info(
            "notes dry-run %s (%s): %d news, has_financials=%s",
            ticker, week_iso, len(context["news"]),
            context["financials"] is not None,
        )
        return result

    if client is None and not settings.has_llm:
        result.llm_disabled = True
        log.warning("notes %s: no ANTHROPIC_API_KEY, skipping", ticker)
        return result

    today = utcnow().date()
    with connect(_db_path()) as conn:
        remaining = spend_repo.remaining_micros(
            conn, settings.notes_daily_cap_cents, today, table="note_spend"
        )
    if remaining <= 0:
        result.budget_exhausted = True
        log.warning("notes %s: daily cap already spent", ticker)
        return result

    try:
        markdown, usage, model_id = _call_model(context, client=client)
    except llm_svc.LLMResponseError as e:
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
                day=today,
                table="note_spend",
            )
        log.warning("notes %s (%s) failed: %s", ticker, week_iso, e)
        return result
    except Exception as e:  # transport / SDK error — no billing
        result.failed = True
        result.reason = f"transport_error: {e}"
        log.warning("notes %s (%s) failed (no billing): %s", ticker, week_iso, e)
        return result

    note = AnalystNote(
        ticker=ticker,
        week_start=week_iso,
        model=model_id,
        title=_title_from_markdown(markdown),
        markdown=markdown,
        context_json=json.dumps(context, ensure_ascii=False),
        input_tokens=usage.input_tokens
        + usage.cache_read_tokens
        + usage.cache_write_tokens,
        output_tokens=usage.output_tokens,
        usd_micros=usage.usd_micros,
        generated_utc=utc_iso(),
    )
    with connect(_db_path()) as conn:
        notes_repo.upsert(conn, note)
        spend_repo.add_usage(
            conn,
            input_tokens=note.input_tokens,
            output_tokens=note.output_tokens,
            usd_micros=note.usd_micros,
            day=today,
            table="note_spend",
        )
    result.note = note
    result.usage = usage
    log.info(
        "notes %s (%s): %d in / %d out ($%.4f) via %s",
        ticker, week_iso, note.input_tokens, note.output_tokens,
        note.usd_micros / 1_000_000, model_id,
    )
    return result


def generate_for_all(
    *,
    week_start: date | None = None,
    client: Any | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    delay_between_s: float | None = None,
) -> PassCounts:
    """Walk every active equity, generating a note per ticker. Idempotent
    within the same week via the store's `INSERT OR REPLACE`. Budget cap
    is checked before every ticker's call — once crossed, remaining
    tickers are counted as `skipped_budget`."""
    week_start = week_start or iso_week_monday()
    counts = PassCounts(week_start=week_start.isoformat())
    delay = settings.notes_delay_between_s if delay_between_s is None else delay_between_s

    with connect(_db_path()) as conn:
        equities = sec_repo.list_by_kind(conn, "equity")
    tickers = [r["ticker"] for r in equities]
    if limit is not None:
        tickers = tickers[:limit]
    counts.considered = len(tickers)

    for i, ticker in enumerate(tickers):
        result = generate_for_ticker(
            ticker, week_start=week_start, client=client, dry_run=dry_run,
        )
        if result.dry_run:
            counts.dry_run_count += 1
        elif result.note is not None:
            counts.generated += 1
            counts.tickers_generated.append(ticker)
            if result.usage:
                counts.total_usd_micros += result.usage.usd_micros
        elif result.budget_exhausted:
            counts.skipped_budget += 1
        elif result.failed:
            counts.failed += 1
            counts.tickers_failed.append(ticker)
        # Polite pause between calls, except on the last one and in dry runs.
        if delay > 0 and not dry_run and i < len(tickers) - 1:
            time.sleep(delay)

    log.info("notes pass %s: %s", counts.week_start, counts.as_dict())
    return counts


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def latest_note(ticker: str) -> AnalystNote | None:
    with connect(_db_path()) as conn:
        return notes_repo.latest_for_ticker(conn, ticker.upper())


def get_note(ticker: str, week_start: str | date) -> AnalystNote | None:
    w = week_start if isinstance(week_start, str) else week_start.isoformat()
    with connect(_db_path()) as conn:
        return notes_repo.get(conn, ticker.upper(), w)


def list_notes(ticker: str, *, limit: int = 12) -> list[AnalystNote]:
    with connect(_db_path()) as conn:
        return notes_repo.list_for_ticker(conn, ticker.upper(), limit=limit)


def spent_today_micros(day: date | None = None) -> int:
    day = day or utcnow().date()
    with connect(_db_path()) as conn:
        return spend_repo.spent_micros(conn, day, table="note_spend")


__all__ = [
    "PassCounts",
    "TickerResult",
    "gather_context",
    "generate_for_all",
    "generate_for_ticker",
    "get_note",
    "iso_week_monday",
    "latest_note",
    "list_notes",
    "spent_today_micros",
]
