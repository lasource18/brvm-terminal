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
import math
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
from brvm.services import company as company_svc
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
    skipped_no_change: bool = False
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
            (self.skipped_no_change, "skipped_no_change"),
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
    skipped_no_change: int = 0
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
            "skipped_no_change": self.skipped_no_change,
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


def _max_drawdown_pct(closes_newest_first: list[float]) -> float | None:
    """Peak-to-trough decline magnitude over the window (positive %).

    Walks chronologically so the "peak" ratchet only sees prior prices.
    A monotonically-rising series returns 0.0; a series with insufficient
    data returns None so the caller can suppress the field rather than
    reporting a meaningless zero.
    """
    if len(closes_newest_first) < 2:
        return None
    peak = closes_newest_first[-1]  # earliest = walk start
    worst = 0.0
    for px in reversed(closes_newest_first):
        if px > peak:
            peak = px
        if peak > 0:
            dd = (peak - px) / peak
            if dd > worst:
                worst = dd
    return worst * 100.0


def _log_returns(closes_newest_first: list[float]) -> list[float]:
    """Daily log returns from a newest-first close series. `log(newer /
    older)` so a positive return means the price went up over the day."""
    rets: list[float] = []
    for i in range(len(closes_newest_first) - 1):
        older = closes_newest_first[i + 1]
        newer = closes_newest_first[i]
        if older and newer:
            rets.append(math.log(newer / older))
    return rets


def _beta_vs_market(
    stock_bars: list,
    market_bars: list | None,
) -> float | None:
    """Regression beta of the stock vs the market over aligned sessions.

    Aligns by `session_date` so a public-holiday gap on one side (or a
    fresh IPO whose window doesn't extend to the market's earliest bar)
    doesn't stretch or mis-pair returns. Both series come in newest-
    first from `history.get_history`; the same convention flows through
    the log-return computation, so the sign of `beta` matches Bloomberg /
    textbook: positive when the two move together.

    Returns None when there aren't at least 20 aligned daily returns —
    beta on a two-week sample is noise, and callers prefer a missing
    field to a suspicious number.
    """
    if not market_bars or not stock_bars:
        return None
    market_by_date = {b.session_date: b.close for b in market_bars if b.close is not None}
    if not market_by_date:
        return None
    aligned_stock: list[float] = []
    aligned_market: list[float] = []
    for b in stock_bars:
        if b.close is None:
            continue
        m_close = market_by_date.get(b.session_date)
        if m_close is None:
            continue
        aligned_stock.append(b.close)
        aligned_market.append(m_close)

    stock_rets = _log_returns(aligned_stock)
    market_rets = _log_returns(aligned_market)
    n = min(len(stock_rets), len(market_rets))
    if n < 20:
        return None
    stock_rets = stock_rets[:n]
    market_rets = market_rets[:n]

    market_var = statistics.pvariance(market_rets)
    if market_var <= 0:
        return None
    mean_s = statistics.fmean(stock_rets)
    mean_m = statistics.fmean(market_rets)
    cov = sum(
        (s - mean_s) * (m - mean_m) for s, m in zip(stock_rets, market_rets, strict=True)
    ) / n
    return cov / market_var


def _price_stats(
    bars: list, market_bars: list | None = None
) -> dict[str, Any] | None:
    """Compressed price action summary from up to 90 daily bars —
    a full series would be wasted tokens for the model.

    `market_bars` (typically BRVMC's 90-day series) unlocks the
    `beta_vs_market` field via `_beta_vs_market`. Missing / short
    market series simply suppresses that field; the rest of the payload
    is computed from the stock's own closes.
    """
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
    max_dd = _max_drawdown_pct(closes)
    if max_dd is not None:
        payload["max_drawdown_pct"] = max_dd
    if len(closes) >= 5:
        # Simple realised vol proxy: stdev of daily log returns x sqrt(252).
        rets = _log_returns(closes)
        if len(rets) >= 2:
            payload["annualised_vol_pct"] = statistics.pstdev(rets) * math.sqrt(252) * 100
    beta = _beta_vs_market(bars, market_bars)
    if beta is not None:
        payload["beta_vs_market"] = beta
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


_PEER_MEDIAN_FIELDS: tuple[str, ...] = (
    "pe", "roe", "net_margin", "change_ytd_pct", "market_cap",
)


def _peer_medians(rows: list[dict[str, Any]]) -> dict[str, float]:
    """Median of each numeric peer field over non-None values.

    Fields with fewer than 2 non-None samples are omitted so Sonnet
    can't misread a one-peer sample as a stable sector reference.
    Returns an empty dict when no field clears the floor — the prompt
    then falls back to "no peer median available" rather than inventing
    a normal range.
    """
    out: dict[str, float] = {}
    for field_name in _PEER_MEDIAN_FIELDS:
        values = [r[field_name] for r in rows if r.get(field_name) is not None]
        if len(values) >= 2:
            out[field_name] = statistics.median(values)
    return out


def _peers_lite(ticker: str) -> dict[str, Any]:
    """Peer list for competitive positioning. Best-effort: an outbound
    HTTP failure downgrades to an empty list rather than aborting the
    whole note.

    The `medians` block is computed here (not by Sonnet) because a
    Python median beats letting the model eyeball a small sample —
    without it, notes reliably fell back to vague "normal range"
    language instead of concrete comparisons.
    """
    try:
        view = company_svc.get_peers_with_ratios(ticker)
    except Exception as e:
        log.warning("peers fetch failed for %s: %s", ticker, e)
        return {"sector": None, "source": "none", "peers": [], "medians": {}}
    self_row: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    for p in view.peers:
        row = {
            "ticker": p.ticker,
            "name": p.name,
            "country": p.country,
            "last": p.last,
            "change_ytd_pct": p.change_ytd_pct,
            "market_cap": p.market_cap,
            "pe": p.pe,
            "roe": p.roe,
            "net_margin": p.net_margin,
        }
        if p.is_self:
            self_row = row
            continue
        rows.append(row)
    payload: dict[str, Any] = {
        "sector": view.sector,
        "source": view.source,
        "peers": rows,
        "medians": _peer_medians(rows),
    }
    if self_row is not None:
        # Include the subject's own ratios in the payload so Sonnet can
        # do a direct self-vs-median compare without having to look them
        # up on the top-level `ratios` block.
        payload["self"] = {
            field: self_row.get(field) for field in _PEER_MEDIAN_FIELDS
        }
    return payload


def _corporate_actions_lite(ticker: str, week_start: date, lookback_days: int) -> list[dict[str, Any]]:
    """Corporate actions with ex_date in the note's lookback window OR in
    the near-term upcoming window (next 180 days). Feeds both the model
    (upcoming events to flag as risks) and the change detector (a newly
    announced dividend is a reason to refresh the note)."""
    since_iso = (week_start - timedelta(days=lookback_days)).isoformat()
    horizon_iso = (week_start + timedelta(days=180)).isoformat()
    with connect(_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT id, ticker, kind, ex_date, pay_date, amount, currency,
                   yield_pct, note, first_seen_utc
            FROM corporate_actions
            WHERE ticker = ?
              AND (
                  (ex_date IS NOT NULL AND ex_date BETWEEN ? AND ?)
                  OR (ex_date IS NULL AND first_seen_utc >= ?)
              )
            ORDER BY (ex_date IS NULL), ex_date
            """,
            (ticker, since_iso, horizon_iso, since_iso),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "ex_date": r["ex_date"],
            "pay_date": r["pay_date"],
            "amount": r["amount"],
            "currency": r["currency"],
            "yield_pct": r["yield_pct"],
            "note": r["note"],
        }
        for r in rows
    ]


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
    # The job runs Saturday evening for the just-ended trading week, so
    # the news window must cover the whole covered week — not just up to
    # its Monday. `date_to` in `list_feed` is inclusive end-of-day.
    until = (week_start + timedelta(days=5)).isoformat()
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
    # BRVMC anchors `beta_vs_market`. A cold BRVMC series (fresh DB,
    # no scheduled index snapshot yet) just suppresses the beta field —
    # nothing else in the payload depends on this fetch.
    market_bars = history.get_history("BRVMC")
    market_trimmed = market_bars[:90] if market_bars else []
    ownership = fundamentals.get_ownership(ticker)
    segments = fundamentals.get_segments(ticker)
    peers = _peers_lite(ticker)
    actions = _corporate_actions_lite(ticker, week_start, lookback)

    return {
        "ticker": ticker,
        "week_start": week_start.isoformat(),
        "security": _security_meta(sec_view),
        "quote": _quote_lite(sec_view),
        "price_stats": _price_stats(trimmed, market_trimmed),
        "financials": _financials_lite(fs),
        "interim": _interim_lite(interim),
        "ratios": _ratios_lite(latest_ratios),
        "ownership": [
            {"holder": h["holder"], "share_pct": h["share_pct"]}
            for h in ownership.holders
        ] if ownership.has_data else [],
        "segments": {
            "period_year": segments.period_year,
            "currency": segments.currency,
            "business": segments.business,
            "geo": segments.geo,
        } if segments.has_data else {},
        "peers": peers,
        "corporate_actions": actions,
        "news": [_news_lite(r) for r in feed.items],
    }


# ---------------------------------------------------------------------------
# Change detection — skip a weekly regen when nothing material has moved
# ---------------------------------------------------------------------------


def _context_fingerprint(context: dict[str, Any]) -> dict[str, Any]:
    """Deterministic summary of the inputs that would make a fresh note
    materially different from the previous one. Two contexts with the
    same fingerprint would produce a near-identical note — regenerating
    is spend without new signal.

    Watched inputs (per user request): news, corporate actions, and
    newly-released financials. Peers and prices are intentionally
    excluded — they move constantly and would defeat the whole skip
    mechanism."""
    fin_periods: list[tuple[int, str]] = []
    fin = context.get("financials") or {}
    for row in fin.get("rows", []) or []:
        year = row.get("period_year")
        kind = row.get("period_kind", "annual")
        if year is not None:
            fin_periods.append((int(year), str(kind)))
    interim = context.get("interim") or {}
    interim_key: tuple[int, str] | None = None
    if interim:
        y = interim.get("period_year")
        k = interim.get("period_kind", "")
        if y is not None:
            interim_key = (int(y), str(k))
    return {
        "news_ids": sorted(int(n["id"]) for n in context.get("news") or [] if n.get("id") is not None),
        "financials_periods": sorted(fin_periods),
        "interim_period": interim_key,
        "corporate_action_ids": sorted(
            int(a["id"]) for a in context.get("corporate_actions") or [] if a.get("id") is not None
        ),
    }


def _fingerprint_from_stored(context_json: str | None) -> dict[str, Any] | None:
    if not context_json:
        return None
    try:
        prev = json.loads(context_json)
    except (json.JSONDecodeError, TypeError):
        return None
    return _context_fingerprint(prev)


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
ratios, ownership, segment split, sector peer list with their headline \
ratios, upcoming/recent corporate actions, and the last 30 days of \
Haiku-tagged news. Produce a concise markdown note (~800-1100 words) a \
portfolio manager can read in 3 minutes.

Structure (in this order, using the exact markdown headings):

# Snapshot
2-3 sentences on the company and its recent trading action. Include \
the sector, headline valuation multiple, and the 90-day return. When \
`price_stats.max_drawdown_pct` or `price_stats.beta_vs_market` are \
populated, weave the more notable one into the description of the \
trading action (e.g. a wide drawdown vs the BRVMC, a beta materially \
above or below 1). Skip both if they're absent — never fabricate.

# Recent developments
Bullet list of the most material items from the news snapshot. Group \
tightly-related items on one line; skip low-relevance filler. If there \
is no news, say "no notable news this window".

# Financial position
3-5 sentences reading the latest annual + interim (when present). Call \
out revenue and profit trend, margin trajectory, balance-sheet shape \
(leverage / equity ratio when useful). Never state a figure that isn't \
in the snapshot.

# Competitive positioning
3-5 sentences comparing the company to the peers listed in the \
snapshot's `peers` block. Anchor on the ratios provided (P/E, ROE, net \
margin) and, when the `segments` block is populated, note how the \
company's business mix or geographic split differentiates it from peers. \
Call out where it screens rich or cheap vs the peer set, and whether \
the segment mix explains the gap. If `peers` is empty or every peer's \
ratios are null, say "no peer data available this window" — do not \
invent peers or numbers.

# Ratios read-across
2-3 sentences on how the ratios compare to the sector medians in \
`peers.medians` (computed across the peer list, with fields dropped \
when fewer than 2 peers had a value). Where a median is present, \
compare the company's own value (either from the `ratios` block or \
`peers.self`) to it explicitly with concrete numbers — e.g. \
"P/E 12x vs sector median 9x". Flag anything that stands out (very \
high P/E, negative growth, unusual payout). If `peers.medians` is \
empty, say "no sector median available this window" — do not fall \
back to a generic "normal range" phrasing.

# Risks & watch items
Bullet list of forward-looking risks the reader should track: known \
upcoming events from `corporate_actions` (dividend ex-dates, AGMs, \
capital actions) and from the news, open questions raised by recent \
filings, macro exposures (WAEMU, currency).

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
    force: bool = False,
) -> TickerResult:
    """Generate + persist a note for one ticker's week. Safe to call
    unconditionally — every operational case (no key, budget exhausted,
    non-equity ticker, empty reply, transport error, dry run, no-change
    skip) returns a `TickerResult` with the appropriate flags set.

    Pass `force=True` to bypass the no-change skip and always regenerate.
    A re-run within the same week already overwrites via the store's
    `INSERT OR REPLACE`, so `force` is mainly for prompt-change reruns."""
    ticker = ticker.upper()
    week_start = week_start or iso_week_monday()
    week_iso = week_start.isoformat()
    result = TickerResult(ticker=ticker, week_start=week_iso)

    context = gather_context(ticker, week_start=week_start)
    if context is None:
        result.failed = True
        result.reason = "not_equity_or_unknown_ticker"
        return result

    # No-change skip: if a previous note exists (for any week) and its
    # fingerprint matches the current context — no new news, no new CA,
    # no newly-released financials — regenerating wastes tokens on the
    # same story. The current week's row (from an earlier same-week run)
    # is ignored so a mid-week rerun still refreshes the row it wrote.
    if not force:
        with connect(_db_path()) as conn:
            previous = notes_repo.latest_for_ticker(conn, ticker)
        if previous is not None and previous.week_start != week_iso:
            prev_fp = _fingerprint_from_stored(previous.context_json)
            if prev_fp is not None and prev_fp == _context_fingerprint(context):
                result.skipped_no_change = True
                result.reason = f"no_change_since_{previous.week_start}"
                log.info(
                    "notes %s (%s): no change since %s, skipping",
                    ticker, week_iso, previous.week_start,
                )
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
    force: bool = False,
) -> PassCounts:
    """Walk every active equity, generating a note per ticker. Idempotent
    within the same week via the store's `INSERT OR REPLACE`. Budget cap
    is checked before every ticker's call — once crossed, remaining
    tickers are counted as `skipped_budget`. Tickers with no new news,
    corporate actions, or financials since their previous note are
    counted as `skipped_no_change` (pass `force=True` to override)."""
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
            force=force,
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
        elif result.skipped_no_change:
            counts.skipped_no_change += 1
        elif result.failed:
            counts.failed += 1
            counts.tickers_failed.append(ticker)
        # Polite pause between calls, except on the last one, in dry runs,
        # and after a no-change skip that never touched the network.
        if (
            delay > 0
            and not dry_run
            and not result.skipped_no_change
            and i < len(tickers) - 1
        ):
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
