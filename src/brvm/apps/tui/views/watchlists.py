"""Watchlists view: list + create + delete + item add/remove.

Selecting a watchlist here also switches the sidebar to it. Emits
`WatchlistChanged` so the app can trigger the sidebar refresh.
"""

from __future__ import annotations

import contextlib
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import DataTable, Input, Label

from brvm.services import watchlist


class WatchlistsView(Vertical):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("delete", "delete_selected", "delete watchlist", show=True),
        Binding("n", "focus_new", "new watchlist", show=True),
        Binding("a", "focus_add", "add ticker", show=True),
        # F-06: `r` used to remove the highlighted ticker — shadowing the
        # global `r` = refresh binding, so a habitual refresh press
        # silently deleted rows. Rebound to `x` to leave `r` alone.
        Binding("x", "remove_ticker", "remove ticker", show=True),
    ]

    class WatchlistChanged(Message):
        """Fired after a mutation so the app can reload the sidebar."""

    def __init__(self) -> None:
        super().__init__()
        self._selected_slug: str | None = None

    def compose(self) -> ComposeResult:
        yield Label("Watchlists  (n: new, Del: delete)", classes="home-section-title")
        wl = DataTable(id="wl-list", cursor_type="row", zebra_stripes=True)
        wl.add_columns("Slug", "Name", "Items")
        yield wl

        with Horizontal():
            yield Input(placeholder="new watchlist name…", id="wl-new-name")

        yield Label("Members  (a: add, x: remove)", classes="home-section-title")
        it = DataTable(id="wl-items", cursor_type="row", zebra_stripes=True)
        it.add_columns("Ticker", "Name", "Last", "Chg%")
        yield it
        with Horizontal():
            yield Input(placeholder="ticker to add…", id="wl-add-ticker")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        wl = self.query_one("#wl-list", DataTable)
        w_cursor = wl.cursor_row
        wl.clear()
        lists = watchlist.list_all()
        counts: dict[str, int] = {}
        for w in lists:
            # Cheap `count` — the DB has one item table + a small watchlists set.
            try:
                view = watchlist.get_with_quotes(w.slug)
                counts[w.slug] = len(view.items)
            except watchlist.WatchlistNotFound:
                counts[w.slug] = 0
        for w in lists:
            wl.add_row(w.slug, w.name, str(counts.get(w.slug, 0)), key=w.slug)
        if lists:
            wl.move_cursor(row=min(w_cursor, len(lists) - 1), animate=False)
            if self._selected_slug is None:
                self._selected_slug = lists[0].slug

        self._refresh_items()

    def _refresh_items(self) -> None:
        it = self.query_one("#wl-items", DataTable)
        cursor = it.cursor_row
        it.clear()
        if self._selected_slug is None:
            return
        try:
            view = watchlist.get_with_quotes(self._selected_slug)
        except watchlist.WatchlistNotFound:
            self._selected_slug = None
            return
        from brvm.apps.tui.format import coloured_pct, num
        for row in view.items:
            it.add_row(
                row.ticker,
                (row.name or "")[:30],
                num(row.last, decimals=0),
                coloured_pct(row.change_pct),
                key=row.ticker,
            )
        if view.items:
            it.move_cursor(row=min(cursor, len(view.items) - 1), animate=False)

    # -- actions -----------------------------------------------------------

    def action_focus_new(self) -> None:
        self.query_one("#wl-new-name", Input).focus()

    def action_focus_add(self) -> None:
        self.query_one("#wl-add-ticker", Input).focus()

    def action_delete_selected(self) -> None:
        slug = self._selected_slug
        if slug is None:
            return
        with contextlib.suppress(watchlist.WatchlistNotFound):
            watchlist.delete(slug)
        self._selected_slug = None
        self.refresh_data()
        self.post_message(self.WatchlistChanged())

    def action_remove_ticker(self) -> None:
        slug = self._selected_slug
        if slug is None:
            return
        it = self.query_one("#wl-items", DataTable)
        if it.row_count == 0:
            return
        try:
            key = it.coordinate_to_cell_key(it.cursor_coordinate).row_key.value
        except Exception:
            return
        try:
            watchlist.remove_item(slug, str(key))
        except (watchlist.WatchlistNotFound, watchlist.TickerUnknown):
            return
        self._refresh_items()
        self.post_message(self.WatchlistChanged())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "wl-new-name":
            name = event.input.value.strip()
            if not name:
                return
            try:
                created = watchlist.create(name)
            except watchlist.WatchlistExists as e:
                # F-06: a duplicate slug used to raise IntegrityError up
                # through the input handler and crash the whole app. Surface
                # it as a validation warning instead.
                self.notify(
                    f"a watchlist called {e.slug!r} already exists",
                    severity="warning",
                )
                return
            self._selected_slug = created.slug
            event.input.value = ""
            self.refresh_data()
            self.post_message(self.WatchlistChanged())
        elif event.input.id == "wl-add-ticker":
            slug = self._selected_slug
            if slug is None:
                self.notify("select a watchlist first", severity="warning")
                return
            ticker = event.input.value.strip().upper()
            if not ticker:
                return
            try:
                watchlist.add_item(slug, ticker)
            except watchlist.TickerUnknown:
                self.notify(f"unknown ticker: {ticker}", severity="warning")
                return
            except watchlist.WatchlistNotFound:
                self.notify(f"watchlist {slug!r} vanished", severity="warning")
                return
            event.input.value = ""
            self._refresh_items()
            self.post_message(self.WatchlistChanged())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "wl-list":
            self._selected_slug = str(event.row_key.value)
            self._refresh_items()
