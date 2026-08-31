"""Ctrl-P command palette — type ticker or company name, Enter opens it.

Reuses `services.search.search` so results match the topbar search in
the web app.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, Label, ListItem, ListView

from kodji.services import search as search_svc


class SearchPalette(ModalScreen):
    """Modal search. Emits `PickedTicker` on selection, or dismisses on Esc."""

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "app.pop_screen", "close"),
    ]

    class PickedTicker(Message):
        def __init__(self, ticker: str) -> None:
            self.ticker = ticker
            super().__init__()

    def __init__(self) -> None:
        super().__init__()
        # Parallel to the ListView children — index -> ticker, so we don't
        # need unique widget ids (which prevent showing the same ticker
        # twice, unlikely but possible if search evolves).
        self._tickers: list[str] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-box"):
            yield Label("Search  (type ticker or name, Enter to open)")
            yield Input(placeholder="e.g. SNTS or sonatel", id="palette-input")
            yield ListView(id="palette-list")

    async def on_input_changed(self, event: Input.Changed) -> None:
        lv = self.query_one("#palette-list", ListView)
        # `clear()` is async in Textual >=8; awaiting it guarantees the
        # subsequent `append()` calls see an empty list (no DuplicateIds).
        await lv.clear()
        self._tickers.clear()
        q = event.value.strip()
        if not q:
            return
        for hit in search_svc.search(q, limit=15):
            label = f"{hit.ticker:<8}  {hit.kind:<6}  {hit.name}"
            lv.append(ListItem(Label(label)))
            self._tickers.append(hit.ticker)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # If a list item is highlighted, use that; else the first hit.
        lv = self.query_one("#palette-list", ListView)
        idx = lv.index if lv.index is not None else 0
        if 0 <= idx < len(self._tickers):
            self._pick(self._tickers[idx])

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        lv = self.query_one("#palette-list", ListView)
        idx = lv.children.index(event.item) if event.item in lv.children else -1
        if 0 <= idx < len(self._tickers):
            self._pick(self._tickers[idx])

    def _pick(self, ticker: str) -> None:
        self.app.pop_screen()
        self.app.post_message(self.PickedTicker(ticker))
