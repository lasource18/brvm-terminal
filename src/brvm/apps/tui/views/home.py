"""Home view: indices strip + movers + high-relevance news feed.

Mirrors the web `/` page. Read-only; refresh is idempotent.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Label, Static

from brvm.apps.tui.format import ACCENT, DIM, coloured_pct, num
from brvm.services import market
from brvm.services import news as news_svc


class HomeView(Vertical):
    """Overview dashboard — the default landing view."""

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
            nt.add_columns("When", "Rel", "Category", "Tickers", "Title")
            yield nt

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
        for row in feed.items:
            when = (row.published_at or row.fetched_utc or "")[:16].replace("T", " ")
            nt.add_row(
                when,
                str(row.relevance) if row.relevance is not None else "—",
                row.category or "—",
                ",".join(row.tickers) or "—",
                (row.title or "").strip()[:80],
                key=str(row.id),
            )
        if feed.items:
            nt.move_cursor(row=min(cursor, len(feed.items) - 1), animate=False)


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
