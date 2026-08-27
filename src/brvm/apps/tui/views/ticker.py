"""Per-ticker view: quote header + tabbed content.

Tabs mirror the web `/s/{ticker}` page: Overview, Chart, News,
Financials, Peers, Corporate actions, Analyst view. Index-kind
tickers hide the equity-only tabs (Financials, Peers, Analyst view).
The Daily brief lives on Home now — it's a global summary, not a
per-ticker artefact — so no Brief tab here.

Every non-table tab wraps its body in `VerticalScroll` so long content
(long descriptions, wide financials tables, multi-paragraph markdown
notes) scrolls with the mouse wheel / PgUp / PgDn instead of getting
clipped at the tab area's height.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import DataTable, Markdown, Static, TabbedContent, TabPane
from textual_plotext import PlotextPlot

from brvm.apps.tui.format import ACCENT, DIM, coloured_pct, link_cell, num
from brvm.services import (
    analyst_notes,
    company,
    fundamentals,
    history,
    market,
)
from brvm.services import (
    news as news_svc,
)


class TickerView(Vertical):
    """Renders one security at a time. `set_ticker` swaps the target."""

    def __init__(self) -> None:
        super().__init__()
        self._ticker: str | None = None
        self._sec: object | None = None

    def compose(self) -> ComposeResult:
        yield Static("Select a ticker…", id="quote-header")
        with TabbedContent(id="ticker-tabs"):
            # VerticalScroll gives mouse-wheel + PgUp/PgDn scrolling for
            # tab bodies that grow past the tab area's height. DataTable
            # is already scrollable so its tabs skip the wrapper — nesting
            # DataTable inside another scroll container hijacks the arrow
            # keys used to move cells.
            with TabPane("Overview", id="tab-overview"), VerticalScroll():
                yield Static(id="overview-body")
            with TabPane("Chart", id="tab-chart"):
                # Use textual-plotext's PlotextPlot widget rather than
                # `Static.update(plt.build())` — the latter dumps raw
                # ANSI escape sequences into a Static which renders them
                # as garbage (the "mix of description + pixels" symptom).
                yield PlotextPlot(id="chart-plot")
            with TabPane("News", id="tab-news"):
                news_table = DataTable(id="ticker-news", cursor_type="row", zebra_stripes=True)
                news_table.add_columns("When", "Rel", "Category", "Title", "Link")
                yield news_table
            with TabPane("Financials", id="tab-financials"), VerticalScroll():
                yield Static(id="financials-body")
            with TabPane("Peers", id="tab-peers"):
                peers = DataTable(id="peers-table", cursor_type="row", zebra_stripes=True)
                peers.add_columns("Ticker", "Name", "Last", "YTD%", "P/E", "ROE%", "NetMg%")
                yield peers
            with TabPane("Corp actions", id="tab-actions"):
                actions = DataTable(id="actions-table", cursor_type="row", zebra_stripes=True)
                actions.add_columns("Kind", "Ex date", "Pay date", "Amount", "Yield%", "Note")
                yield actions
            with TabPane("Analyst view", id="tab-analyst"), VerticalScroll():
                yield Markdown("", id="analyst-md")

    def set_ticker(self, ticker: str) -> None:
        self._ticker = ticker.upper()
        self._sec = market.get_security(self._ticker)
        self.refresh_data()

    def refresh_data(self) -> None:
        if self._ticker is None:
            return
        # Re-fetch the SecurityView so the header shows the latest quote.
        self._sec = market.get_security(self._ticker)

        self._render_header()
        self._render_overview()
        self._render_chart()
        self._render_news()
        self._render_financials()
        self._render_peers()
        self._render_actions()
        self._render_analyst()

    # -- individual sections ----------------------------------------------

    def _render_header(self) -> None:
        header = self.query_one("#quote-header", Static)
        if self._sec is None:
            header.update(f"[b]{self._ticker}[/]  not found")
            return
        sec = self._sec
        q = getattr(sec, "quote", None)
        name = getattr(sec, "name", "")
        kind = getattr(sec, "kind", "")
        country = getattr(sec, "country", None) or "—"
        if q is None:
            header.update(
                f"[b {ACCENT}]{sec.ticker}[/]  {name}\n"
                f"[{DIM}]{kind} · {country}  ·  no quote yet[/]"
            )
            return
        header.update(
            f"[b {ACCENT}]{sec.ticker}[/]  {name}\n"
            f"[{DIM}]{kind} · {country}[/]  "
            f"last [b]{num(q.last, decimals=0)}[/]  "
            f"chg {coloured_pct(q.change_pct)}  "
            f"vol {num(q.volume, decimals=0)}  "
            f"turnover {num(q.turnover, decimals=0)} XOF"
        )

    def _render_overview(self) -> None:
        body = self.query_one("#overview-body", Static)
        try:
            profile = company.get_description(self._ticker or "")
        except Exception as e:
            body.update(f"[{DIM}]description unavailable ({e})[/]")
            return
        if profile is None:
            body.update(f"[{DIM}]no description on file[/]")
            return
        pieces: list[str] = []
        if profile.description:
            pieces.append(profile.description)
        meta: list[str] = []
        if profile.sector:
            meta.append(f"Sector: {profile.sector}")
        if profile.industry:
            meta.append(f"Industry: {profile.industry}")
        if profile.address:
            meta.append(f"Address: {profile.address}")
        if profile.phone:
            meta.append(f"Phone: {profile.phone}")
        if profile.website:
            meta.append(f"Web: {profile.website}")
        if profile.leadership:
            meta.append(f"Leadership: {profile.leadership}")
        if profile.shares_outstanding:
            meta.append(f"Shares: {profile.shares_outstanding}")
        if profile.market_cap:
            meta.append(f"Mkt cap: {profile.market_cap}")
        if meta:
            pieces.append("\n".join(meta))
        if profile.shareholders:
            pieces.append("Shareholders:")
            for sh in profile.shareholders[:12]:
                pieces.append(f"  {sh.name}  {sh.pct:.2f}%")
        body.update("\n\n".join(pieces) if pieces else f"[{DIM}]no details on file[/]")

    def _render_chart(self) -> None:
        plot = self.query_one("#chart-plot", PlotextPlot)
        assert self._ticker
        country = getattr(self._sec, "country", None) if self._sec else None
        try:
            bars = history.get_history(self._ticker, country)
        except Exception as e:
            # Fall back to a title-only chart with the error inline so the
            # tab still renders instead of raising through the refresh loop.
            plot.plt.clear_figure()
            plot.plt.title(f"{self._ticker} — chart unavailable: {e}")
            plot.refresh()
            return
        # `get_history` returns newest-first; plotext wants oldest→newest.
        trimmed = list(reversed(bars[:90])) if bars else []
        closes = [b.close for b in trimmed if b.close is not None]
        dates = [b.session_date.isoformat() for b in trimmed if b.close is not None]
        plt = plot.plt
        plt.clear_figure()
        if not closes:
            plt.title(f"{self._ticker} — no history")
            plot.refresh()
            return
        plt.date_form("Y-m-d")
        plt.theme("dark")
        plt.plot(dates, closes, marker="braille")
        plt.title(f"{self._ticker} — last {len(closes)} sessions")
        # PlotextPlot sizes itself to the widget's rect on the next paint;
        # explicit plot_size would fight the layout.
        plot.refresh()

    def _render_news(self) -> None:
        table = self.query_one("#ticker-news", DataTable)
        cursor = table.cursor_row
        table.clear()
        feed = news_svc.list_feed(ticker=self._ticker, limit=50)
        for row in feed.items:
            when = (row.published_at or row.fetched_utc or "")[:16].replace("T", " ")
            table.add_row(
                when,
                str(row.relevance) if row.relevance is not None else "—",
                row.category or "—",
                (row.title or "").strip()[:80],
                link_cell(row.url),
                key=str(row.id),
            )
        if feed.items:
            table.move_cursor(row=min(cursor, len(feed.items) - 1), animate=False)

    def _render_financials(self) -> None:
        body = self.query_one("#financials-body", Static)
        assert self._ticker
        if self._sec and getattr(self._sec, "kind", "") == "index":
            body.update(f"[{DIM}]not applicable for indices[/]")
            return
        fs = fundamentals.get_financials_series(self._ticker)
        interim = fundamentals.get_latest_interim(self._ticker)
        refs = fundamentals.get_financials_source_filings(self._ticker)
        lines: list[str] = []
        if fs.has_data:
            # Header row: metric name, one column per period (newest→oldest).
            years = fs.periods
            hdr = f"{'metric':<20} " + "  ".join(f"{y:>12}" for y in years)
            lines.append(hdr)
            lines.append("-" * len(hdr))
            for key, series in fs.metrics.items():
                vals = "  ".join(f"{num(v, decimals=0):>12}" for v in series)
                lines.append(f"{key:<20} {vals}")
            lines.append(f"\ncurrency: {fs.currency}")
        else:
            lines.append(f"[{DIM}]no annual financials extracted[/]")
        if interim is not None and interim.has_data:
            lines.append(f"\nInterim ({interim.period_kind} {interim.period_year}):")
            for k, v in interim.metrics.items():
                lines.append(f"  {k:<20} {num(v, decimals=0)}")
        if refs:
            lines.append("\nReferences (source filings):")
            for r in refs:
                pub = r.published_date or "—"
                lines.append(
                    f"  {r.period_kind:<6} {r.period_year}  "
                    f"{r.doc_type:<20} {pub:<10}  {r.source_url}"
                )
        body.update("\n".join(lines))

    def _render_peers(self) -> None:
        table = self.query_one("#peers-table", DataTable)
        assert self._ticker
        if self._sec and getattr(self._sec, "kind", "") == "index":
            table.clear()
            return
        try:
            view = company.get_peers_with_ratios(self._ticker)
        except Exception as e:
            self.notify(f"peers fetch failed: {e}", severity="warning")
            return
        cursor = table.cursor_row
        table.clear()
        for p in view.peers:
            marker = "▸ " if p.is_self else "  "
            table.add_row(
                marker + p.ticker,
                (p.name or "")[:24],
                num(p.last, decimals=0),
                coloured_pct(p.change_ytd_pct),
                num(p.pe, decimals=1) if p.pe is not None else "—",
                num(p.roe, decimals=1) if p.roe is not None else "—",
                num(p.net_margin, decimals=1) if p.net_margin is not None else "—",
                key=p.ticker,
            )
        if view.peers:
            table.move_cursor(row=min(cursor, len(view.peers) - 1), animate=False)

    def _render_actions(self) -> None:
        table = self.query_one("#actions-table", DataTable)
        assert self._ticker
        rows = news_svc.list_upcoming_actions(ticker=self._ticker, days=365)
        cursor = table.cursor_row
        table.clear()
        for r in rows:
            table.add_row(
                r.kind,
                r.ex_date.isoformat() if r.ex_date else "—",
                r.pay_date.isoformat() if r.pay_date else "—",
                f"{r.amount} {r.currency or ''}".strip() if r.amount else "—",
                f"{r.yield_pct:.2f}" if r.yield_pct is not None else "—",
                (r.note or "")[:40],
                key=str(r.id),
            )
        if rows:
            table.move_cursor(row=min(cursor, len(rows) - 1), animate=False)

    def _render_analyst(self) -> None:
        md = self.query_one("#analyst-md", Markdown)
        assert self._ticker
        if self._sec and getattr(self._sec, "kind", "") == "index":
            md.update("*Not applicable for indices.*")
            return
        note = analyst_notes.latest_note(self._ticker)
        if note is None:
            md.update(
                f"*No analyst note on file for {self._ticker}. "
                "Run `just analyst-notes-run --ticker "
                f"{self._ticker}`.*"
            )
            return
        md.update(note.markdown or "")
