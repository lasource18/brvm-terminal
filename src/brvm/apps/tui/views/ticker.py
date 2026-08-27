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

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
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
    bonds as bonds_svc,
)
from brvm.services import (
    news as news_svc,
)


class TickerView(Vertical):
    """Renders one security at a time. `set_ticker` swaps the target."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("o", "open_news_url", "open story", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._ticker: str | None = None
        self._sec: object | None = None
        # Row-id → URL for the currently-rendered News tab feed. Populated
        # in `_render_news` so `o` opens the highlighted story.
        self._news_urls: dict[str, str] = {}

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
            # Phase 8c: composite bond DES + CSHF + YAS + REL screen.
            # Only meaningful when kind='bond'; renders "N/A" otherwise
            # so the tab bar stays stable across kinds.
            with TabPane("Bond details", id="tab-bond"), VerticalScroll():
                yield Static(id="bond-body")

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
        self._render_bond()

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
        kind = getattr(self._sec, "kind", "") if self._sec else ""
        if kind == "bond":
            # Bonds share the Overview tab with equities but the payload is
            # the DES reference block. The full CSHF + YAS + REL screen
            # lives on the dedicated Bond details tab.
            self._render_bond_overview(body)
            return
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
        # Bonds don't get equity-ticker tags from the news tagger; the
        # issuer-name substring fallback in `services.bonds` bridges the
        # gap so the tab isn't empty.
        kind = getattr(self._sec, "kind", "") if self._sec else ""
        if kind == "bond":
            rows = bonds_svc.list_issuer_news(self._ticker or "", limit=50)
            feed = news_svc.list_feed_from_rows(rows)
        else:
            feed = news_svc.list_feed(ticker=self._ticker, limit=50)
        self._news_urls = {}
        for row in feed.items:
            when = (row.published_at or row.fetched_utc or "")[:16].replace("T", " ")
            row_key = str(row.id)
            if row.url:
                self._news_urls[row_key] = row.url
            table.add_row(
                when,
                str(row.relevance) if row.relevance is not None else "—",
                row.category or "—",
                (row.title or "").strip()[:80],
                link_cell(row.url),
                key=row_key,
            )
        if feed.items:
            table.move_cursor(row=min(cursor, len(feed.items) - 1), animate=False)

    def _render_financials(self) -> None:
        body = self.query_one("#financials-body", Static)
        assert self._ticker
        kind = getattr(self._sec, "kind", "") if self._sec else ""
        if kind == "index":
            body.update(f"[{DIM}]not applicable for indices[/]")
            return
        if kind == "bond":
            body.update(f"[{DIM}]not applicable for bonds — see the Bond details tab[/]")
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
        kind = getattr(self._sec, "kind", "") if self._sec else ""
        if kind in {"index", "bond"}:
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
        # Phase 8g: median + mean summary rows underneath the peer list so
        # the TUI matches the web tab's Bloomberg-style cross-compare.
        # DataTable's `key` must be unique — use a synthetic key so a
        # subsequent refresh doesn't collide with a real ticker.
        stats = getattr(view, "stats", {}) or {}
        if stats:
            pe_s = stats.get("pe")
            roe_s = stats.get("roe")
            nm_s = stats.get("net_margin")
            ytd_s = stats.get("change_ytd_pct")
            table.add_row(
                "  ─ MEDIAN",
                "(peers)",
                "—",
                coloured_pct(ytd_s.median) if ytd_s and ytd_s.median is not None else "—",
                num(pe_s.median, decimals=1) if pe_s and pe_s.median is not None else "—",
                num(roe_s.median, decimals=1) if roe_s and roe_s.median is not None else "—",
                num(nm_s.median, decimals=1) if nm_s and nm_s.median is not None else "—",
                key="__peer_median__",
            )
            table.add_row(
                "  ─ MEAN",
                "(peers)",
                "—",
                coloured_pct(ytd_s.mean) if ytd_s and ytd_s.mean is not None else "—",
                num(pe_s.mean, decimals=1) if pe_s and pe_s.mean is not None else "—",
                num(roe_s.mean, decimals=1) if roe_s and roe_s.mean is not None else "—",
                num(nm_s.mean, decimals=1) if nm_s and nm_s.mean is not None else "—",
                key="__peer_mean__",
            )
        if view.peers:
            table.move_cursor(row=min(cursor, len(view.peers) - 1), animate=False)

    def _render_actions(self) -> None:
        table = self.query_one("#actions-table", DataTable)
        assert self._ticker
        # Bonds pay coupons, not dividends — the schedule lives on Bond
        # details. Blank the equity Corporate actions table so it doesn't
        # show a stale ex-div row from another ticker.
        kind = getattr(self._sec, "kind", "") if self._sec else ""
        if kind == "bond":
            table.clear()
            return
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
        kind = getattr(self._sec, "kind", "") if self._sec else ""
        if kind == "index":
            md.update("*Not applicable for indices.*")
            return
        if kind == "bond":
            md.update("*Not applicable for bonds — analyst notes are per-equity.*")
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

    def _render_bond(self) -> None:
        """Populate the Bond details tab. Renders "N/A" for non-bonds so
        the tab bar stays stable across kinds — Textual TabbedContent
        doesn't expose a clean hide-tab API mid-lifecycle."""
        body = self.query_one("#bond-body", Static)
        assert self._ticker
        kind = getattr(self._sec, "kind", "") if self._sec else ""
        if kind != "bond":
            body.update(f"[{DIM}]This tab is only meaningful for bonds.[/]")
            return
        view = bonds_svc.get_bond_view(self._ticker)
        if view is None:
            body.update(f"[{DIM}]bond details unavailable[/]")
            return

        lines: list[str] = []
        # Yield / duration block first — it's the summary a scanner
        # wants at the top.
        if view.yield_:
            y = view.yield_
            lines.append(f"[b {ACCENT}]Yield & Duration[/]")
            lines.append(
                f"  YTM {num(y.ytm_pct, decimals=2)}%  "
                f"CurYld {num(y.current_yield_pct, decimals=2)}%"
            )
            lines.append(
                f"  ModDur {num(y.modified_duration_years, decimals=2)} yrs  "
                f"MacDur {num(y.macaulay_duration_years, decimals=2)} yrs  "
                f"Convex {num(y.convexity, decimals=2)}"
            )
            lines.append(
                f"  Clean {num(y.clean_price, decimals=2)}  "
                f"Accrued {num(y.accrued_coupon, decimals=2)}  "
                f"Dirty {num(y.dirty_price, decimals=2)}"
            )
            lines.append("")

        # Cash-flow schedule.
        if view.schedule:
            s = view.schedule
            lines.append(
                f"[b {ACCENT}]Cash flow[/]  "
                f"({s.coupons_remaining} left, nominal "
                f"{num(s.nominal, decimals=0)} XOF, bullet + annual)"
            )
            hdr = f"  {'DATE':<12} {'COUPON':>12} {'PRINCIPAL':>12} {'TOTAL':>12}"
            lines.append(hdr)
            lines.append("  " + "-" * (len(hdr) - 2))
            for r in s.rows:
                principal = num(r.principal, decimals=0) if r.principal > 0 else "—"
                lines.append(
                    f"  {r.payment_date.isoformat():<12} "
                    f"{num(r.coupon, decimals=2):>12} "
                    f"{principal:>12} "
                    f"{num(r.total, decimals=2):>12}"
                )
            lines.append("")
        else:
            lines.append(f"[{DIM}]Cash-flow schedule unavailable (missing coupon anchor).[/]")
            lines.append("")

        # Related bonds.
        if view.related:
            lines.append(f"[b {ACCENT}]Related bonds[/]  (same issuer)")
            hdr = f"  {'TICKER':<10} {'COUPON':>7}  {'MATURITY':>9}  NAME"
            lines.append(hdr)
            lines.append("  " + "-" * (len(hdr) - 2))
            for r in view.related:
                coupon = f"{r.coupon_rate:.2f}%" if r.coupon_rate is not None else "—"
                my = str(r.maturity_year) if r.maturity_year else "—"
                matured = "  · matured" if r.is_matured else ""
                lines.append(
                    f"  {r.ticker:<10} {coupon:>7}  {my:>9}  {r.name[:60]}{matured}"
                )
            lines.append("")

        # Nice-to-haves.
        if view.issuer_equity_ticker:
            lines.append(
                f"[b]Issuer equity:[/] {view.issuer_equity_ticker}"
            )
        if view.prospectus_news:
            lines.append(f"[b {ACCENT}]Prospectus / admission news[/]")
            for n in view.prospectus_news:
                when = (n.published_at or "")[:10] or "—"
                lines.append(f"  {when}  {n.title[:80]}")

        body.update("\n".join(lines) if lines else f"[{DIM}]no bond data on file[/]")

    def _render_bond_overview(self, body: Static) -> None:
        """Populate the Overview tab body for a bond ticker."""
        view = bonds_svc.get_bond_view(self._ticker or "")
        if view is None:
            body.update(f"[{DIM}]bond details unavailable[/]")
            return
        coupon = f"{view.coupon_rate:.2f}%" if view.coupon_rate is not None else "—"
        maturity = str(view.maturity_year) if view.maturity_year else "—"
        issue = view.issue_date.isoformat() if view.issue_date else "—"
        lines: list[str] = [
            f"[b {ACCENT}]{view.ticker}[/]  {view.name}",
            f"  Issuer:   {view.issuer_name or '—'}",
            f"  Category: {view.sector or '—'}"
            + (f"  ·  {view.country}" if view.country else ""),
            f"  Coupon:   {coupon}  (annual)",
            f"  Issue:    {issue}",
            f"  Maturity: {maturity}",
        ]
        if view.last_snapshot:
            lc = view.last_snapshot.last_coupon_date
            la = view.last_snapshot.last_coupon_amount
            lines.append(
                f"  Accrued:  {num(view.last_snapshot.accrued_coupon, decimals=2)}"
            )
            if lc:
                lines.append(
                    f"  Last pmt: {lc.isoformat()}"
                    + (f"  · {num(la, decimals=2)}" if la is not None else "")
                )
        if view.yield_:
            lines.append("")
            lines.append(
                f"  Current yield: {num(view.yield_.current_yield_pct, decimals=2)}%  "
                f"YTM: {num(view.yield_.ytm_pct, decimals=2)}%"
            )
        if view.schedule and view.schedule.next_coupon_date:
            lines.append(
                f"  Next coupon:   {view.schedule.next_coupon_date.isoformat()}  "
                f"({view.schedule.coupons_remaining} left)"
            )
        if view.issuer_equity_ticker:
            lines.append("")
            lines.append(f"  Issuer equity: [b]{view.issuer_equity_ticker}[/]")
        body.update("\n".join(lines))

    # -- bindings ---------------------------------------------------------

    def action_open_news_url(self) -> None:
        """Open the currently-highlighted News tab row in the browser.

        DataTable eats mouse clicks for row selection so an OSC-8 click on
        the visible "open" link is unreliable across terminals — this
        binding is the reliable path. No-op unless the News tab is active
        and a URL is on file for the row."""
        table = self.query_one("#ticker-news", DataTable)
        if table.row_count == 0:
            return
        try:
            row_key = table.coordinate_to_cell_key(
                (table.cursor_row, 0)
            ).row_key.value
        except Exception:
            return
        url = self._news_urls.get(str(row_key)) if row_key else None
        if not url:
            self.notify("No source URL on file for this row.", severity="warning")
            return
        self.app.open_url(url)
