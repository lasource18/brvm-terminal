"""Textual TUI shell over the BRVM services layer (Phase 5).

Reads through the same services the web app uses; the TUI is a viewer,
not a fetcher — refresh polls the local SQLite. Market-hours-aware
polling paused at 30s / off-hours no-op via `brvm.clock.is_market_open`.
"""
