"""Shared cell formatters for the TUI.

Numbers, percentages, timestamps — one place so the visual language
stays consistent across every table and quote header.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from brvm.clock import ABIDJAN

# Rich markup colours used by the TUI. Kept in one place so a theme
# swap is grep-and-replace.
UP = "bright_green"
DOWN = "bright_red"
DIM = "grey58"
MUTED = "grey42"
ACCENT = "yellow"


def num(v: float | int | None, *, decimals: int = 0) -> str:
    """Space-thousands number, matching the French convention. `—` for None."""
    if v is None:
        return "—"
    if decimals == 0:
        s = f"{round(v):,}".replace(",", " ")
    else:
        s = f"{v:,.{decimals}f}".replace(",", " ")
    return s


def pct(v: float | None, *, decimals: int = 2, signed: bool = True) -> str:
    if v is None:
        return "—"
    return f"{v:+.{decimals}f}%" if signed else f"{v:.{decimals}f}%"


def coloured_pct(v: float | None) -> str:
    """Rich-markup percentage: green up, red down, dim for None/zero."""
    if v is None:
        return f"[{DIM}]—[/]"
    if v > 0:
        return f"[{UP}]{v:+.2f}%[/]"
    if v < 0:
        return f"[{DOWN}]{v:+.2f}%[/]"
    return f"[{DIM}]0.00%[/]"


def xof(v: float | None) -> str:
    """Money-shaped XOF amount."""
    return num(v, decimals=0)


def parse_iso_utc(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def age(iso: str | None, *, now: datetime | None = None) -> str:
    """`Xs ago` / `Xm ago` / `Xh ago` — the footer clock signature."""
    dt = parse_iso_utc(iso)
    if dt is None:
        return "—"
    now = now or datetime.now(tz=UTC)
    delta = (now - dt).total_seconds()
    if delta < 0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def local_hm(iso: str | None, *, tz: str = "Africa/Abidjan") -> str:
    """`HH:MM` in the requested tz. Silent `—` on missing/malformed input."""
    dt = parse_iso_utc(iso)
    if dt is None:
        return "—"
    z = ABIDJAN if tz == "Africa/Abidjan" else ZoneInfo(tz)
    return dt.astimezone(z).strftime("%H:%M")


def link_cell(url: str | None, *, label: str = "open"):
    """DataTable cell that opens `url` in the terminal's browser.

    Rich `Text` with a `link` style renders as an OSC-8 hyperlink escape
    sequence — every modern terminal (iTerm2, Kitty, Alacritty, WezTerm,
    macOS Terminal 2.14+, VSCode) makes it clickable. Older terminals
    just show the label as underlined text; the URL isn't lost.

    Building a `Text` object (not a `[link=...]` markup string) sidesteps
    Rich markup escaping — URLs frequently contain `[` in query strings.
    """
    from rich.text import Text

    if not url:
        return "—"
    return Text(label, style=f"link {url}", overflow="fold")
