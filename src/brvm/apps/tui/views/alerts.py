"""Alerts view: recent events + rules editor.

Two stacked tables — top is the events inbox (most recent 100), bottom
is the rules list with keyboard-driven toggle/delete. Rule creation is
a small in-view input row (kind + ticker + threshold_pct / min_relevance
depending on kind).
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Input, Label, Select

from brvm.models import AlertRule
from brvm.services import alerts as alerts_svc

_KIND_OPTIONS = [
    ("price_move", "price_move"),
    ("new_filing", "new_filing"),
    ("news", "news"),
]


class AlertsView(Vertical):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("t", "toggle_rule", "toggle rule", show=True),
        Binding("delete", "delete_rule", "delete rule", show=True),
        Binding("n", "focus_new", "new rule", show=True),
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
            yield Input(placeholder="doc_types csv (new_filing only)", id="new-doctypes")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        et = self.query_one("#alerts-events", DataTable)
        e_cursor = et.cursor_row
        et.clear()
        for ev in alerts_svc.list_recent_events(limit=100):
            fired = (ev.fired_utc or "")[:19].replace("T", " ")
            et.add_row(
                fired,
                ev.kind,
                ev.ticker or "—",
                ev.delivery_status or "—",
                (ev.subject or "")[:80],
                key=str(ev.id),
            )
        events = alerts_svc.list_recent_events(limit=1)
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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Only the "new rule" row triggers rule creation; any submit in it
        # commits the whole row.
        if event.input.id not in {"new-ticker", "new-arg", "new-doctypes"}:
            return
        kind = str(self.query_one("#new-kind", Select).value)
        ticker = self.query_one("#new-ticker", Input).value.strip().upper() or None
        arg_raw = self.query_one("#new-arg", Input).value.strip()
        doctypes = self.query_one("#new-doctypes", Input).value.strip() or None

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

        rule = AlertRule(
            kind=kind,
            ticker=ticker,
            threshold_pct=threshold,
            min_relevance=min_rel,
            doc_types=doctypes if kind == "new_filing" else None,
            enabled=True,
        )
        alerts_svc.create_rule(rule)
        for input_id in ("new-ticker", "new-arg", "new-doctypes"):
            self.query_one(f"#{input_id}", Input).value = ""
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
