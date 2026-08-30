"""Main Textual app: shell chrome + sidebar + content switcher.

Persistent chrome:
- Header row with market status + Abidjan clock (bar via `chrome-row`).
- Left sidebar with watchlist picker + selected list's ticker table.
- Right pane is a `ContentSwitcher` that swaps between the six views.
- Footer surfaces keybindings.

Refresh model: `set_interval(30)` during market hours (paused via
`clock.is_market_open()`), also on `r`. Every view exposes
`refresh_data()`; only the currently-visible view + the sidebar are
polled — repaints preserve `DataTable.cursor_coordinate`.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import ContentSwitcher, Footer, Header, Static

from brvm.apps.tui.news_ticker import NewsTicker
from brvm.apps.tui.palette import SearchPalette
from brvm.apps.tui.sidebar import Sidebar
from brvm.apps.tui.views.alerts import AlertsView
from brvm.apps.tui.views.directory import DirectoryView
from brvm.apps.tui.views.home import HomeView
from brvm.apps.tui.views.news import NewsView
from brvm.apps.tui.views.ticker import TickerView
from brvm.apps.tui.views.watchlists import WatchlistsView
from brvm.clock import is_market_open, now_abidjan
from brvm.services import market as market_svc

REFRESH_SECONDS = 30.0


class BRVMTerminalApp(App):
    """The main Textual app."""

    CSS_PATH = Path(__file__).with_name("style.tcss")
    TITLE = "brvm-terminal"
    SUB_TITLE = "Bloomberg-ish"

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("h", "show('home')", "home"),
        Binding("d", "show('directory')", "directory"),
        Binding("f5", "show('news')", "news"),
        Binding("w", "show('watchlists')", "watchlists"),
        Binding("a", "show('alerts')", "alerts"),
        Binding("t", "open_ticker_view", "ticker"),
        Binding("ctrl+k", "search", "search"),
        Binding("r", "refresh_now", "refresh"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._current_view: str = "home"
        self._last_snapshot: str | None = None

    def compose(self) -> ComposeResult:
        # `n` reaches the news filter input (`/`) via the News view's own
        # binding; we deliberately don't bind `n` at the app level so the
        # user can type search text without opening News.
        yield Header(show_clock=False)
        with Horizontal(id="chrome-row"):
            yield Static("", id="market-status")
            yield Static("", id="last-updated")
            yield Static("", id="clock")
        with Horizontal(id="body"):
            yield Sidebar()
            # Assign each child a stable id keyed on the `show(...)` binding.
            # ContentSwitcher matches on `id`, so drift here breaks navigation.
            with ContentSwitcher(id="right-pane", initial="home"):
                home = HomeView()
                home.id = "home"
                yield home
                ticker = TickerView()
                ticker.id = "ticker"
                yield ticker
                directory = DirectoryView()
                directory.id = "directory"
                yield directory
                news = NewsView()
                news.id = "news"
                yield news
                watchlists = WatchlistsView()
                watchlists.id = "watchlists"
                yield watchlists
                alerts = AlertsView()
                alerts.id = "alerts"
                yield alerts
        # Phase 8j: one-line news ticker between the body and the Footer.
        # Rotates through top-relevance news during market hours; pauses
        # (but stays on screen with the last headline) once the market
        # closes.
        yield NewsTicker()
        yield Footer()

    def on_mount(self) -> None:
        self._chrome_tick = 0
        self._paint_chrome()
        self.set_interval(1.0, self._paint_chrome)
        self.set_interval(REFRESH_SECONDS, self._tick_refresh)

    # -- chrome ------------------------------------------------------------

    def _paint_chrome(self) -> None:
        clock = self.query_one("#clock", Static)
        clock.update(f"{now_abidjan().strftime('%Y-%m-%d %H:%M:%S')} Abidjan")

        status = self.query_one("#market-status", Static)
        market_open = is_market_open()
        if market_open:
            status.update("[b]● OPEN[/]  ")
            status.remove_class("-closed")
        else:
            status.update("[b]○ CLOSED[/]  ")
            status.add_class("-closed")
        # Phase 8j: pause the ticker rotation off-hours so a reader
        # coming back after close doesn't see the pool loop against
        # a stale market.
        with contextlib.suppress(Exception):  # pragma: no cover - defensive
            self.query_one(NewsTicker).set_paused(not market_open)

        last = self.query_one("#last-updated", Static)
        # F-37: `last_snapshot_utc` is a rare-write field (updated by
        # the snapshot job every 10 min during market hours, hourly
        # otherwise). Re-querying every second means ~86 400 idle
        # `MAX(captured_utc)` scans per day. Cache it and refresh
        # from the DB every 10 chrome ticks (10 s) — the age
        # rendering still updates every second off the cached value.
        self._chrome_tick += 1
        if self._chrome_tick % 10 == 1:
            try:
                self._last_snapshot = market_svc.last_snapshot_utc()
            except Exception:
                self._last_snapshot = None
        from brvm.apps.tui.format import age

        last.update(f"last snapshot: {age(self._last_snapshot)}")

    def _tick_refresh(self) -> None:
        # Off-hours we skip the poll to stay quiet — the manual `r` binding
        # is always available.
        if not is_market_open():
            return
        self._do_refresh()

    def _do_refresh(self) -> None:
        try:
            self.query_one(Sidebar).refresh_data()
        except Exception as e:
            self.log.warning(f"sidebar refresh failed: {e}")
        for view in self._visible_views():
            try:
                view.refresh_data()
            except Exception as e:
                self.log.warning(f"view refresh failed: {e}")

    def _visible_views(self) -> list:
        cs = self.query_one("#right-pane", ContentSwitcher)
        current_id = cs.current
        if not current_id:
            return []
        try:
            return [cs.get_child_by_id(current_id)]
        except Exception:
            return []

    # -- actions -----------------------------------------------------------

    def action_show(self, view_id: str) -> None:
        cs = self.query_one("#right-pane", ContentSwitcher)
        cs.current = view_id
        self._current_view = view_id

    def action_search(self) -> None:
        self.push_screen(SearchPalette())

    def action_open_ticker_view(self) -> None:
        """`t` from anywhere. If a ticker was previously loaded the view
        already has content — just switch back to it. Otherwise open the
        search palette so the user picks one instead of landing on an
        empty screen."""
        self.action_show("ticker")
        try:
            tv = self.query_one(TickerView)
        except Exception:
            return
        if not tv.has_ticker:
            self.push_screen(SearchPalette())

    def action_refresh_now(self) -> None:
        self._do_refresh()
        self._paint_chrome()

    # -- cross-view messages ----------------------------------------------

    def on_sidebar_ticker_selected(self, event: Sidebar.TickerSelected) -> None:
        self._open_ticker(event.ticker)

    def on_directory_view_ticker_selected(self, event: DirectoryView.TickerSelected) -> None:
        self._open_ticker(event.ticker)

    def on_news_view_ticker_selected(self, event: NewsView.TickerSelected) -> None:
        self._open_ticker(event.ticker)

    def on_search_palette_picked_ticker(self, event: SearchPalette.PickedTicker) -> None:
        self._open_ticker(event.ticker)

    def on_ticker_view_header_clicked(self, event: TickerView.HeaderClicked) -> None:
        """Clicking the quote header opens the search palette so a
        pointer-only user can pick a ticker without knowing ctrl+k."""
        del event
        self.push_screen(SearchPalette())

    def on_watchlists_view_watchlist_changed(
        self, event: WatchlistsView.WatchlistChanged
    ) -> None:
        with contextlib.suppress(Exception):
            self.query_one(Sidebar).reload_and_refresh()

    def _open_ticker(self, ticker: str) -> None:
        try:
            tv = self.query_one(TickerView)
        except Exception:
            return
        tv.set_ticker(ticker)
        self.action_show("ticker")
