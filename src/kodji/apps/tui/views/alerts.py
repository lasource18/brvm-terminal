"""Alerts view: recent events + rules editor.

Two stacked tables — top is the events inbox (most recent 100), bottom
is the rules list with keyboard-driven toggle/delete. Rule creation is
a small in-view input row (kind + ticker + threshold_pct / min_relevance
depending on kind).
"""

from __future__ import annotations

from typing import ClassVar, get_args

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, Label, Select, SelectionList

from kodji.models import AlertRule, FilingDocType
from kodji.services import alerts as alerts_svc

_KIND_OPTIONS = [
    ("price_move", "price_move"),
    ("new_filing", "new_filing"),
    ("news", "news"),
]

# Phase 8j: the FilingDocType Literal drives the SelectionList so a new
# doc_type is picked up without duplicating the tuple here.
_DOC_TYPE_VALUES: tuple[str, ...] = tuple(get_args(FilingDocType))
# `SelectionList` expects `(prompt, value, initial_state)` triples.
# Prompts are the same string as the value — the tab uses machine-
# readable identifiers rather than French labels so a CSV round-trip
# through `AlertRule.doc_types` stays lossless.
_DOC_TYPE_ITEMS: list[tuple[str, str]] = [(v, v) for v in _DOC_TYPE_VALUES]


class AlertsView(Vertical):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("t", "toggle_rule", "toggle rule", show=True),
        Binding("delete", "delete_rule", "delete rule", show=True),
        Binding("n", "focus_new", "new rule", show=True),
        # Same escape-to-blur affordance as the Watchlists view — Input
        # consumes most keys, so a `priority=True` binding is needed for
        # the escape to fire while a child input has focus.
        Binding("escape", "blur_input", "unfocus", show=False, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Label("Recent events", classes="home-section-title")
        events = DataTable(id="alerts-events", cursor_type="row", zebra_stripes=True)
        events.add_columns("Fired", "Kind", "Ticker", "Delivery", "Subject")
        yield events

        yield Label("Rules  (t: toggle, Del: delete, n: new)", classes="home-section-title")
        rules = DataTable(id="alerts-rules", cursor_type="row", zebra_stripes=True)
        rules.add_columns("ID", "On", "Kind", "Ticker", "Threshold", "MinRel", "DocTypes")
        yield rules

        with Horizontal(id="alerts-new-row"):
            yield Select(_KIND_OPTIONS, id="new-kind", allow_blank=False, value="price_move")
            yield Input(placeholder="ticker (blank = all)", id="new-ticker")
            yield Input(placeholder="threshold_pct / min_relevance", id="new-arg")
            # Phase 8j: `SelectionList` replaces the CSV-shaped Input for
            # doc_types when kind == 'new_filing'. Space toggles a
            # selection, arrows navigate. Read via `.selected` at submit
            # time — no manual CSV parsing on the caller side. Kept
            # visible for every kind (Textual doesn't ship an inline
            # multi-select combo, and hiding it on kind-change would
            # complicate the compose tree); ignored at submit when the
            # kind isn't `new_filing`.
            yield SelectionList[str](*_DOC_TYPE_ITEMS, id="new-doctypes")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        et = self.query_one("#alerts-events", DataTable)
        e_cursor = et.cursor_row
        et.clear()
        # F-35: clamp against the actual events list we just rendered,
        # not a stray second `limit=1` query — the old shape always
        # collapsed the cursor to row 0 on every 30-second refresh.
        events = alerts_svc.list_recent_events(limit=100)
        for ev in events:
            fired = (ev.fired_utc or "")[:19].replace("T", " ")
            et.add_row(
                fired,
                ev.kind,
                ev.ticker or "—",
                ev.delivery_status or "—",
                (ev.subject or "")[:80],
                key=str(ev.id),
            )
        if events:
            et.move_cursor(row=min(e_cursor, len(events) - 1), animate=False)

        rt = self.query_one("#alerts-rules", DataTable)
        r_cursor = rt.cursor_row
        rt.clear()
        rules = alerts_svc.list_rules()
        for rule in rules:
            rt.add_row(
                str(rule.id),
                "yes" if rule.enabled else "no",
                rule.kind,
                rule.ticker or "*",
                _fmt_threshold(rule.threshold_pct),
                str(rule.min_relevance) if rule.min_relevance is not None else "—",
                rule.doc_types or "—",
                key=str(rule.id),
            )
        if rules:
            rt.move_cursor(row=min(r_cursor, len(rules) - 1), animate=False)

    # -- actions -----------------------------------------------------------

    def action_toggle_rule(self) -> None:
        rid = self._selected_rule_id()
        if rid is None:
            return
        rules = {r.id: r for r in alerts_svc.list_rules()}
        rule = rules.get(rid)
        if rule is None:
            return
        alerts_svc.set_enabled(rid, not rule.enabled)
        self.refresh_data()

    def action_delete_rule(self) -> None:
        rid = self._selected_rule_id()
        if rid is None:
            return
        alerts_svc.delete_rule(rid)
        self.refresh_data()

    def action_focus_new(self) -> None:
        self.query_one("#new-ticker", Input).focus()

    def action_blur_input(self) -> None:
        """Escape → focus the events table so the app's shortcuts (h,
        d, w, t, r, …) fire again."""
        try:
            self.query_one("#alerts-events", DataTable).focus()
        except Exception:  # pragma: no cover - defensive
            self.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Only the "new rule" row triggers rule creation; any submit in it
        # commits the whole row.
        if event.input.id not in {"new-ticker", "new-arg"}:
            return
        kind = str(self.query_one("#new-kind", Select).value)
        ticker = self.query_one("#new-ticker", Input).value.strip().upper() or None
        arg_raw = self.query_one("#new-arg", Input).value.strip()
        # Phase 8j: doc_types come from the SelectionList's checked items
        # joined into a CSV — the canonical shape `AlertRule.doc_types`
        # persists. Reading `.selected` on a fresh SelectionList returns
        # `[]` so no doc_types → None down below.
        doctypes_list = list(self.query_one("#new-doctypes", SelectionList).selected)
        doctypes = ",".join(doctypes_list) if doctypes_list else None

        threshold = None
        min_rel = None
        if kind == "price_move":
            try:
                threshold = float(arg_raw) if arg_raw else None
            except ValueError:
                self.notify(f"threshold_pct must be a number, got {arg_raw!r}", severity="warning")
                return
            if threshold is None:
                self.notify("price_move needs a threshold_pct", severity="warning")
                return
        elif kind == "news":
            try:
                min_rel = int(arg_raw) if arg_raw else 6
            except ValueError:
                self.notify(f"min_relevance must be an int, got {arg_raw!r}", severity="warning")
                return
        elif kind == "new_filing" and not doctypes_list:
            self.notify(
                "new_filing needs at least one doc_type — space to select",
                severity="warning",
            )
            return

        rule = AlertRule(
            kind=kind,
            ticker=ticker,
            threshold_pct=threshold,
            min_relevance=min_rel,
            doc_types=doctypes if kind == "new_filing" else None,
            enabled=True,
        )
        alerts_svc.create_rule(rule)
        for input_id in ("new-ticker", "new-arg"):
            self.query_one(f"#{input_id}", Input).value = ""
        self.query_one("#new-doctypes", SelectionList).deselect_all()
        self.refresh_data()

    # -- helpers -----------------------------------------------------------

    def _selected_rule_id(self) -> int | None:
        rt = self.query_one("#alerts-rules", DataTable)
        if rt.row_count == 0:
            return None
        try:
            key = rt.coordinate_to_cell_key(rt.cursor_coordinate).row_key.value
        except Exception:
            return None
        try:
            return int(str(key))
        except (TypeError, ValueError):
            return None


def _fmt_threshold(v: float | None) -> str:
    if v is None:
        return "—"
    return f"{v:+.2f}%"
