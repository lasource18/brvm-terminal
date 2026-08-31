"""Directory view: full securities table with period-return columns."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import DataTable, Label

from kodji.apps.tui.format import coloured_pct, num
from kodji.services import directory

# Kept in one place — column list drives both the header and the sort
# cycling. Order matches the web `/directory`.
_COLS: list[tuple[str, str]] = [
    ("ticker", "Ticker"),
    ("name", "Name"),
    ("kind", "Kind"),
    ("country", "Ctry"),
    ("sector", "Sector"),
    ("last", "Last"),
    ("change_pct", "Chg%"),
    ("change_1w_pct", "1W"),
    ("change_1m_pct", "1M"),
    ("change_3m_pct", "3M"),
    ("change_ytd_pct", "YTD"),
    ("change_1y_pct", "1Y"),
    ("change_all_pct", "ALL"),
]

_NUMERIC_COLS = {
    "last", "change_pct", "change_1w_pct", "change_1m_pct",
    "change_3m_pct", "change_ytd_pct", "change_1y_pct", "change_all_pct",
}


class DirectoryView(Vertical):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("s", "cycle_sort", "cycle sort col", show=True),
        Binding("f", "flip_dir", "flip asc/desc", show=True),
    ]

    class TickerSelected(Message):
        def __init__(self, ticker: str) -> None:
            self.ticker = ticker
            super().__init__()

    def __init__(self) -> None:
        super().__init__()
        self._sort_idx: int = -1  # -1 = default order
        self._direction: str = "desc"

    def compose(self) -> ComposeResult:
        yield Label("Directory  (s: cycle sort, f: flip dir, Enter: open)", classes="home-section-title")
        table = DataTable(id="directory-table", cursor_type="row", zebra_stripes=True)
        table.add_columns(*[label for _, label in _COLS])
        yield table

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        sort_key: str | None = None
        if 0 <= self._sort_idx < len(_COLS):
            sort_key = _COLS[self._sort_idx][0]
        rows = directory.list_directory(sort=sort_key, direction=self._direction)

        table = self.query_one("#directory-table", DataTable)
        cursor = table.cursor_row
        table.clear()
        for r in rows:
            table.add_row(
                r.ticker,
                (r.name or "")[:32],
                r.kind or "—",
                r.country or "—",
                (r.sector or "—")[:22],
                num(r.last, decimals=0),
                coloured_pct(r.change_pct),
                coloured_pct(r.change_1w_pct),
                coloured_pct(r.change_1m_pct),
                coloured_pct(r.change_3m_pct),
                coloured_pct(r.change_ytd_pct),
                coloured_pct(r.change_1y_pct),
                coloured_pct(r.change_all_pct),
                key=r.ticker,
            )
        if rows:
            table.move_cursor(row=min(cursor, len(rows) - 1), animate=False)

    def action_cycle_sort(self) -> None:
        # -1 (default) → 0, 1, ..., len-1, -1
        self._sort_idx = self._sort_idx + 1
        if self._sort_idx >= len(_COLS):
            self._sort_idx = -1
        # Numeric columns default to desc (biggest first); text asc.
        if self._sort_idx >= 0:
            key = _COLS[self._sort_idx][0]
            self._direction = "desc" if key in _NUMERIC_COLS else "asc"
        self.refresh_data()

    def action_flip_dir(self) -> None:
        self._direction = "asc" if self._direction == "desc" else "desc"
        self.refresh_data()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        ticker = str(event.row_key.value)
        self.post_message(self.TickerSelected(ticker))
