"""Home view: indices strip + movers + high-relevance news feed +
collapsible daily brief.

Mirrors the web `/` page. Read-only; refresh is idempotent.

The daily brief is a *global* summary (movers + top-tagged news across
the whole market), not a per-ticker artefact, so it lives here rather
than on each `/s/{ticker}` page. Wrapped in `Collapsible` so it stays
out of the way — one line by default, expands to the full markdown on
click.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Collapsible, DataTable, Label, Markdown, Static

from brvm.apps.tui.format import ACCENT, DIM, coloured_pct, link_cell, num
from brvm.services import brief as brief_svc
from brvm.services import market
from brvm.services import news as news_svc


class HomeView(Vertical):
    """Overview dashboard — the default landing view."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("o", "open_news_url", "open story", show=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        # Row-id → URL for the currently-rendered news feed, so the
        # `o` binding can open the highlighted row without re-querying.
        self._news_urls: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Static(id="home-indices")
        with Horizontal(classes="home-section"):
            with Vertical():
                yield Label("Gainers", classes="home-section-title")
                gt = DataTable(id="home-gainers", cursor_type="row", zebra_stripes=True)
                gt.add_columns("Ticker", "Last", "Chg%", "Vol")
                yield gt
            with Vertical():
                yield Label("Losers", classes="home-section-title")
                lt = DataTable(id="home-losers", cursor_type="row", zebra_stripes=True)
                lt.add_columns("Ticker", "Last", "Chg%", "Vol")
                yield lt
        with Vertical(classes="home-section"):
            yield Label("Top news (high relevance)", classes="home-section-title")
            nt = DataTable(id="home-news", cursor_type="row", zebra_stripes=True)
            nt.add_columns("When", "Rel", "Category", "Tickers", "Title", "Link")
            yield nt
        # Collapsed by default so the fold-first area stays terse — the
        # brief is a long-form artefact and would otherwise crowd the
        # movers + news feed above the fold.
        yield Static(id="home-brief-summary", classes="home-brief-summary")
        with (
            Collapsible(title="Daily brief", collapsed=True, id="home-brief"),
            VerticalScroll(id="home-brief-scroll"),
        ):
            yield Markdown("", id="home-brief-md")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        ov = market.overview(limit=8)

        # Indices strip
        indices_widget = self.query_one("#home-indices", Static)
        parts: list[str] = []
        for t in ov.indices:
            level = num(t.level, decimals=2) if t.level is not None else "—"
            parts.append(f"[b {ACCENT}]{t.ticker}[/] {level}  {coloured_pct(t.change_pct)}")
        indices_widget.update("   ".join(parts) if parts else f"[{DIM}]no index data[/]")

        # Gainers + losers
        gt = self.query_one("#home-gainers", DataTable)
        lt = self.query_one("#home-losers", DataTable)
        _refill_movers(gt, ov.gainers)
        _refill_movers(lt, ov.losers)

        # News
        feed = news_svc.list_feed(min_relevance=6, limit=10)
        nt = self.query_one("#home-news", DataTable)
        cursor = nt.cursor_row
        nt.clear()
        self._news_urls = {}
        for row in feed.items:
            when = (row.published_at or row.fetched_utc or "")[:16].replace("T", " ")
            row_key = str(row.id)
            if row.url:
                self._news_urls[row_key] = row.url
            nt.add_row(
                when,
                str(row.relevance) if row.relevance is not None else "—",
                row.category or "—",
                ",".join(row.tickers) or "—",
                (row.title or "").strip()[:80],
                link_cell(row.url),
                key=row_key,
            )
        if feed.items:
            nt.move_cursor(row=min(cursor, len(feed.items) - 1), animate=False)

        # Daily brief — one-line summary in the always-visible header,
        # full markdown inside the collapsible body.
        summary = self.query_one("#home-brief-summary", Static)
        body_md = self.query_one("#home-brief-md", Markdown)
        latest = brief_svc.latest_brief()
        if latest is None:
            summary.update(
                f"[{DIM}]Daily brief · not yet generated "
                "(runs Mon-Fri post-close)[/]"
            )
            body_md.update(
                "*No brief on file. Run `just brief-run` after the market "
                "closes to generate one.*"
            )
        else:
            summary.update(
                f"[{DIM}]Daily brief · [{ACCENT}]{latest.day}[/] · "
                "expand below to read[/]"
            )
            body_md.update(f"# Daily brief — {latest.day}\n\n{latest.markdown or ''}")


    def action_open_news_url(self) -> None:
        """Open the highlighted news row's story URL in the default browser.

        Textual's DataTable intercepts mouse clicks for row selection so an
        OSC-8 hyperlink click inside a cell never reaches the terminal;
        this binding is the reliable path. Falls back to a notification
        when no URL is on file for the row (e.g. an older communiqué)."""
        nt = self.query_one("#home-news", DataTable)
        if nt.row_count == 0:
            return
        try:
            row_key = nt.coordinate_to_cell_key(
                (nt.cursor_row, 0)
            ).row_key.value
        except Exception:
            return
        url = self._news_urls.get(str(row_key)) if row_key else None
        if not url:
            self.notify("No source URL on file for this row.", severity="warning")
            return
        self.app.open_url(url)


def _refill_movers(table: DataTable, rows) -> None:
    cursor = table.cursor_row
    table.clear()
    for r in rows:
        table.add_row(
            r.ticker,
            num(r.last, decimals=0),
            coloured_pct(r.change_pct),
            num(r.volume, decimals=0),
            key=r.ticker,
        )
    if rows:
        table.move_cursor(row=min(cursor, len(rows) - 1), animate=False)
