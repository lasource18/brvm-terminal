"""Left column: watchlist picker + selected watchlist's ticker table.

Reuses the services layer directly (`services.watchlist`, plus a virtual
"turnover leaders" default when no user watchlists exist). Repaints
preserve cursor and scroll offset so the 30s timer doesn't yank the
user around.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, cast

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Label

from kodji.apps.tui.format import coloured_pct, num
from kodji.services import market, watchlist
from kodji.services.accounts import DEFAULT_ACCOUNT_ID

# Sentinel slug — never collides with real slugs, which are lowercased and
# hyphenated versions of user names.
TURNOVER_LEADERS_SLUG = "__turnover_leaders__"
TURNOVER_LEADERS_NAME = "Turnover leaders"


@dataclass(frozen=True)
class SidebarSource:
    slug: str
    name: str
    editable: bool  # False for the virtual "turnover leaders" default


class Sidebar(Vertical):
    """Persistent watchlist column."""

    BINDINGS: ClassVar[list[Binding]] = [
        # F-06: `shift+w` never fired — terminals deliver the literal
        # character `"W"` and Textual doesn't rewrite it into a shift+w
        # binding. Binding the raw uppercase key does what a reader
        # actually expects when they press Shift+W.
        Binding("W", "cycle_watchlist", "next watchlist", show=False),
    ]

    class TickerSelected(Message):
        """Fired when the user selects a ticker row (Enter or click)."""

        def __init__(self, ticker: str) -> None:
            self.ticker = ticker
            super().__init__()

    def __init__(self) -> None:
        super().__init__(id="sidebar")
        self._sources: list[SidebarSource] = []
        self._active_idx: int = 0

    def compose(self) -> ComposeResult:
        yield Label("", id="sidebar-title")
        table = DataTable(id="sidebar-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Ticker", "Last", "Chg%")
        yield table

    def on_mount(self) -> None:
        self._reload_sources()
        self.refresh_data()

    # -- data --------------------------------------------------------------

    def _reload_sources(self) -> None:
        user = watchlist.list_all(DEFAULT_ACCOUNT_ID)
        sources: list[SidebarSource] = []
        # Turnover leaders always available — a fresh install without any
        # user watchlists still shows something meaningful.
        sources.append(
            SidebarSource(
                slug=TURNOVER_LEADERS_SLUG,
                name=TURNOVER_LEADERS_NAME,
                editable=False,
            )
        )
        for w in user:
            sources.append(SidebarSource(slug=w.slug, name=w.name, editable=True))
        self._sources = sources
        if self._active_idx >= len(sources):
            self._active_idx = 0

    @property
    def active_source(self) -> SidebarSource:
        return self._sources[self._active_idx]

    def refresh_data(self) -> None:
        """Repaint the ticker table, preserving cursor position."""
        table = self.query_one("#sidebar-table", DataTable)
        title = self.query_one("#sidebar-title", Label)

        src = self.active_source
        title.update(f"{src.name}  ({self._active_idx + 1}/{len(self._sources)})")

        # Snapshot cursor + scroll to restore after repaint.
        cursor_row = table.cursor_row
        scroll_y = table.scroll_y

        rows = self._rows_for_source(src)
        table.clear()
        for r in rows:
            table.add_row(
                r["ticker"],
                num(r["last"], decimals=0),
                coloured_pct(r["change_pct"]),
                key=r["ticker"],
            )

        # Preserve cursor if still in range; else clamp.
        if rows:
            new_cursor = min(cursor_row, len(rows) - 1)
            table.move_cursor(row=new_cursor, animate=False)
            table.scroll_y = scroll_y

    def _rows_for_source(self, src: SidebarSource) -> list[dict]:
        if src.slug == TURNOVER_LEADERS_SLUG:
            return [
                {
                    "ticker": r.ticker,
                    "last": r.last,
                    "change_pct": r.change_pct,
                }
                for r in market.top_by_turnover(limit=20)
            ]
        try:
            view = watchlist.get_with_quotes(DEFAULT_ACCOUNT_ID, src.slug)
        except watchlist.WatchlistNotFound:
            # Deleted from under us — drop it and re-render turnover leaders.
            self._reload_sources()
            self._active_idx = 0
            return self._rows_for_source(self.active_source)
        return [
            {"ticker": it.ticker, "last": it.last, "change_pct": it.change_pct}
            for it in view.items
        ]

    # -- actions -----------------------------------------------------------

    def action_cycle_watchlist(self) -> None:
        if not self._sources:
            return
        self._active_idx = (self._active_idx + 1) % len(self._sources)
        self.refresh_data()

    def reload_and_refresh(self) -> None:
        """Called after a watchlist mutation (add/remove/create)."""
        self._reload_sources()
        self.refresh_data()

    # -- events ------------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        ticker = cast(str, event.row_key.value)
        self.post_message(self.TickerSelected(ticker))
