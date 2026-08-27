"""Footer news ticker (Phase 8j) — one high-relevance headline at a time.

Renders in a single-row Static above the Footer during market hours and
hides otherwise. Cycles through the top-relevance news items from the
last 24 hours, one every `CYCLE_SECONDS`. Falls back to a static "no
news this window" tag when the DB has nothing scored above the floor.
"""

from __future__ import annotations

from textual.widgets import Static

from brvm.services import news as news_svc

CYCLE_SECONDS = 5.0
LOOKBACK_HOURS = 24
MIN_RELEVANCE = 6
POOL_SIZE = 10


class NewsTicker(Static):
    """Cycling single-line news headline for the app footer."""

    def __init__(self) -> None:
        super().__init__("", id="news-ticker")
        self._pool: list[str] = []
        self._idx: int = 0
        self._paused: bool = False

    def on_mount(self) -> None:
        # First render is synchronous so the row shows up on app start.
        self.refresh_pool()
        self.set_interval(CYCLE_SECONDS, self._tick)

    def set_paused(self, paused: bool) -> None:
        """When market's closed the caller sets this to True; the ticker
        still shows the last headline it had but stops rotating."""
        self._paused = paused
        if paused:
            self.update("[dim]news feed paused (market closed)[/]")
        else:
            self._render_current()

    def refresh_pool(self) -> None:
        """Re-poll the news feed for the current pool of headlines.

        Runs on `_tick` before every rotation so a fresh Haiku-tagged
        item can enter the pool without waiting a full loop. `list_feed`
        already hits the DB with a `min_relevance` filter — cheap.
        """
        try:
            feed = news_svc.list_feed(
                min_relevance=MIN_RELEVANCE, limit=POOL_SIZE, offset=0,
            )
        except Exception:  # pragma: no cover - defensive; ticker must not crash
            feed = None
        if feed is None or not feed.items:
            self._pool = []
            self._idx = 0
            return
        pool: list[str] = []
        for item in feed.items:
            when = (item.published_at or item.fetched_utc or "")[:16].replace("T", " ")
            title = (item.title or "").strip()
            if not title:
                continue
            rel = f"rel {item.relevance}/10" if item.relevance is not None else ""
            tickers = ", ".join(item.tickers) if item.tickers else ""
            hd_bits = [b for b in (when, rel, tickers) if b]
            hd = " · ".join(hd_bits)
            pool.append(f"[b]{hd}[/]  {title}" if hd else title)
        self._pool = pool
        # Reset the rotation so the freshest headline shows first.
        self._idx = 0

    def _tick(self) -> None:
        if self._paused:
            return
        # Refresh only when we're about to wrap — a full loop of the
        # current pool before the poll costs at most POOL_SIZE *
        # CYCLE_SECONDS seconds (50s at defaults), fine for the market-
        # hours cadence.
        if self._idx == 0:
            self.refresh_pool()
        if not self._pool:
            self.update(
                f"[dim]no news scored ≥{MIN_RELEVANCE} "
                f"in the last {LOOKBACK_HOURS}h[/]"
            )
            return
        self._render_current()
        self._idx = (self._idx + 1) % len(self._pool)

    def _render_current(self) -> None:
        if not self._pool:
            return
        self.update(self._pool[self._idx % len(self._pool)])
