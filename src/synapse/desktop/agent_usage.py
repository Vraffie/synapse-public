"""Usage ledger table for the agent run view (GET /api/v1/runs/{id}/usage).

The ledger is provider-reported and distinguishes coverage per field: a value the provider did not report is
``None`` and is shown as "unknown", never as 0. Daemon text is untrusted: ids are cleaned and shortened, counts
are validated as non-negative integers and the number of rows is bounded.
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QGroupBox, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout

from synapse.desktop.agent_view import clean

MAX_ROWS = 200
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
FIELDS = TOKEN_FIELDS + ("cost_usd",)
HEADERS = ("Task", "Attempt", "Input", "Output", "Cache read", "Cache write", "Cost USD (estimate)")
UNKNOWN = "unknown"
EMPTY_TEXT = "No usage has been recorded for this run yet. Unknown is not zero: nothing is shown until the provider reports it."
UNAVAILABLE_USAGE_TEXT = "Usage is not available for this run."
FAILED_USAGE_TEXT = "Usage could not be read right now."
INTRO = "Provider-reported, not independently measured. \"unknown\" means not reported, never zero."


def _num(value, *, cost: bool):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError
    if cost:
        try:
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError
            return float(value)
        except OverflowError:
            return None  # too large for a float: treat as unknown
    if not isinstance(value, int) or value < 0:
        raise ValueError
    return value


def sanitize_usage(raw) -> dict | None:
    """Validate and bound a usage response; None means 'not understood'."""
    try:
        if not isinstance(raw, dict) or not isinstance(raw.get("observations"), list):
            return None
        total = raw.get("attempts_total")
        with_usage = raw.get("attempts_with_usage")
        if type(total) is not int or type(with_usage) is not int or total < 0 or with_usage < 0:
            return None
        rows = []
        for obs in raw["observations"][:MAX_ROWS]:
            if not isinstance(obs, dict):
                return None
            row = {"task_id": clean(str(obs.get("task_id") or ""), 64), "attempt_id": clean(str(obs.get("attempt_id") or ""), 64)}
            for field in FIELDS:
                row[field] = _num(obs.get(field), cost=field == "cost_usd")
            rows.append(row)
        totals_raw, coverage_raw = raw.get("totals") or {}, raw.get("coverage") or {}
        if not isinstance(totals_raw, dict) or not isinstance(coverage_raw, dict):
            return None
        totals, coverage = {}, {}
        for field in FIELDS:
            totals[field] = _num(totals_raw.get(field), cost=field == "cost_usd")
            cov = coverage_raw.get(field)
            if cov is None:
                coverage[field] = None
                continue
            if not isinstance(cov, dict) or type(cov.get("known")) is not int or type(cov.get("attempts")) is not int:
                return None
            if not 0 <= cov["known"] <= cov["attempts"]:
                coverage[field] = "inconsistent"
                totals[field] = None
                continue
            coverage[field] = (cov["known"], cov["attempts"])
        return {"attempts_total": total, "attempts_with_usage": with_usage, "rows": rows, "rows_total": len(raw["observations"]),
                "totals": totals, "coverage": coverage}
    except (ValueError, TypeError):
        return None


def format_value(value, field: str) -> str:
    if value is None:
        return UNKNOWN
    return f"{value:.4f}" if field == "cost_usd" else str(value)


def format_total(usage: dict, field: str) -> str:
    if usage["coverage"].get(field) == "inconsistent":
        return "unknown (inconsistent coverage)"
    value = usage["totals"].get(field)
    if value is None:
        return UNKNOWN
    text = format_value(value, field)
    cov = usage["coverage"].get(field)
    if cov is not None and cov[0] < cov[1]:
        text += f" (lower bound, {cov[0]}/{cov[1]} attempts)"
    return text


class UsagePane(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Usage", parent)
        self.intro = QLabel(INTRO)
        self.intro.setWordWrap(True)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(list(HEADERS))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(160)
        self.table.setAccessibleName("Usage ledger")
        self.table.setAccessibleDescription("Provider-reported token and cost usage per task attempt; unknown means not reported.")
        for label in (self.intro, self.status):
            label.setTextFormat(Qt.TextFormat.PlainText)
        self.totals = QLabel("")
        self.totals.setWordWrap(True)
        self.totals.setTextFormat(Qt.TextFormat.PlainText)
        layout = QVBoxLayout(self)
        for widget in (self.intro, self.status, self.table, self.totals):
            layout.addWidget(widget)
        self.clear()

    def clear(self, status: str = ""):
        self.status.setText(status)
        self.table.setRowCount(0)
        self.table.setVisible(False)
        self.totals.setText("")

    def set_unavailable(self):
        self.clear(UNAVAILABLE_USAGE_TEXT)

    def set_failed(self):
        self.clear(FAILED_USAGE_TEXT)

    def set_usage(self, usage: dict):
        if not usage["rows"]:
            self.clear(EMPTY_TEXT + f" Attempts: {usage['attempts_total']}.")
            return
        extra = usage["rows_total"] - len(usage["rows"])
        self.status.setText(f"Attempts with reported usage: {usage['attempts_with_usage']} of {usage['attempts_total']}."
                            + (f" Showing the first {len(usage['rows'])} of {usage['rows_total']} rows." if extra > 0 else ""))
        self.table.setVisible(True)
        self.table.setRowCount(len(usage["rows"]))
        for r, row in enumerate(usage["rows"]):
            cells = [row["task_id"][:12], row["attempt_id"][:12]] + [format_value(row[f], f) for f in FIELDS]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if c < 2:
                    item.setToolTip(row["task_id" if c == 0 else "attempt_id"])
                self.table.setItem(r, c, item)
        self.totals.setText("Totals (known values only): " + "; ".join(
            f"{label} {format_total(usage, f)}" for label, f in zip(HEADERS[2:], FIELDS)))
