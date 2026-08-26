"""News feed view: filterable list, Enter opens the ticker view."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import DataTable, Input, Label

from brvm.services import news as news_svc


class NewsView(Vertical):
    """News + communiqués with in-view filters."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("/", "focus_search", "filter", show=True),
    ]

    class TickerSelected(Message):
        def __init__(self, ticker: str) -> None:
            self.ticker = ticker
            super().__init__()

    def __init__(self) -> None:
        super().__init__()
        self._ticker: str | None = None
        self._category: str | None = None
        self._min_rel: int | None = None
        self._row_ticker: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Label("News  (/: filter, Enter: open ticker)", classes="home-section-title")
        with Horizontal(id="news-filters"):
            yield Input(placeholder="ticker (blank = all)", id="news-ticker")
            yield Input(placeholder="category (earnings/dividend/...)", id="news-category")
            yield Input(placeholder="min relevance 0-10", id="news-min-rel")
        table = DataTable(id="news-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("When", "Rel", "Category", "Tickers", "Title")
        yield table

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        feed = news_svc.list_feed(
            ticker=self._ticker,
            category=self._category,
            min_relevance=self._min_rel,
            limit=100,
        )
        table = self.query_one("#news-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        # Keep the primary ticker for row-open — first entry in `tickers`,
        # else the LLM-attributed one.
        self._row_ticker: dict[str, str] = {}
        for row in feed.items:
            when = (row.published_at or row.fetched_utc or "")[:16].replace("T", " ")
            key = str(row.id)
            primary = row.tickers[0] if row.tickers else ""
            self._row_ticker[key] = primary
            table.add_row(
                when,
                str(row.relevance) if row.relevance is not None else "—",
                row.category or "—",
                ",".join(row.tickers) or "—",
                (row.title or "").strip()[:80],
                key=key,
            )
        if feed.items:
            table.move_cursor(row=min(cursor, len(feed.items) - 1), animate=False)

    # -- actions -----------------------------------------------------------

    def action_focus_search(self) -> None:
        self.query_one("#news-ticker", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # All three filter inputs share the same submission handler.
        self._ticker = self.query_one("#news-ticker", Input).value.strip().upper() or None
        cat = self.query_one("#news-category", Input).value.strip().lower() or None
        # `list_feed` quietly drops unknown categories, so no client-side check.
        self._category = cat
        rel_raw = self.query_one("#news-min-rel", Input).value.strip()
        try:
            self._min_rel = int(rel_raw) if rel_raw else None
        except ValueError:
            self._min_rel = None
        self.refresh_data()
        self.query_one("#news-table", DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = str(event.row_key.value)
        ticker = self._row_ticker.get(key, "")
        if ticker:
            self.post_message(self.TickerSelected(ticker))
