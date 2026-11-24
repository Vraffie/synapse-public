"""'Repositories' pane for multi-repository runs (ADR 026): per-repository status, diff and the candidate manifest.

Read only: there is no approve, build or deploy action here. Every string comes from ``multirepo_model`` already
sanitized and bounded; labels are additionally forced to plain text and table cells are non-editable plain items,
so markup in a repository id, reason or diff is shown literally and never interpreted.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView, QGroupBox, QLabel, QPlainTextEdit, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from synapse.desktop.multirepo_model import manifest_summary, short

COLUMNS = ("Repository", "Access", "Baseline", "Candidate", "State", "Deploy")


def _label(word_wrap: bool = True) -> QLabel:
    label = QLabel("")
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setWordWrap(word_wrap)
    return label


class MultiRepoPane(QGroupBox):
    def __init__(self, parent=None):
        super().__init__("Repositories", parent)
        self.status = _label()
        self.manifest = _label()
        self.banner = _label()
        self.banner.setStyleSheet("font-weight: bold;")
        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setMaximumHeight(180)
        self.detail = _label()
        self.diff_note = _label()
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.diff.setMaximumHeight(200)
        layout = QVBoxLayout(self)
        for widget in (self.status, self.manifest, self.banner, self.table, self.detail, self.diff_note, self.diff):
            layout.addWidget(widget)
        self._rows = []
        self._shown_repo = None
        self.table.currentCellChanged.connect(lambda row, *_: self._show_row(row))
        self.clear()

    def clear(self):
        self._rows = []
        self._shown_repo = None
        self.table.setRowCount(0)
        for label in (self.status, self.manifest, self.banner, self.detail, self.diff_note):
            label.setText("")
        self.banner.setVisible(False)
        self.diff.setPlainText("")
        self.setVisible(False)

    def set_view(self, view: dict | None):
        if not view or not view.get("visible"):
            self.clear()
            return
        self.setVisible(True)
        rows = view["rows"]
        writes = sum(1 for r in rows if r["access"] == "write")
        status = f"{len(rows)} repositories in this run ({writes} written, {len(rows) - writes} read only)"
        if view.get("dropped"):
            status += f"; {view['dropped']} malformed or extra entries not shown"
        self.status.setText(status)
        headline, banner = manifest_summary(view.get("manifest"))
        self.manifest.setText(headline)
        self.banner.setText(banner)
        self.banner.setVisible(bool(banner))
        keep = self._shown_repo
        self._rows = rows
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = (row["repo_id"], row["access"], f"{row['baseline_ref'] or '-'} @ {short(row['baseline_sha'])}",
                      short(row["candidate_sha"]), row["label"], row["deploy"])
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                self.table.setItem(index, column, item)
        self.table.blockSignals(False)
        ids = [r["repo_id"] for r in rows]
        target = ids.index(keep) if keep in ids else next((i for i, r in enumerate(rows) if r["state"] == "changed"), 0 if rows else -1)
        if target >= 0:
            self.table.blockSignals(True)
            self.table.setCurrentCell(target, 0)
            self.table.blockSignals(False)
        self._show_row(target)

    def _show_row(self, index: int):
        if not 0 <= index < len(self._rows):
            self._shown_repo = None
            self.detail.setText("")
            self.diff_note.setText("")
            self.diff.setPlainText("")
            return
        row = self._rows[index]
        self._shown_repo = row["repo_id"]
        parts = [f"{row['repo_id']}: {row['label']}"]
        if row["branch"]:
            parts.append(f"attempt branch {row['branch']}")
        if row["changed_paths"] is not None:
            parts.append(f"{row['changed_paths']} changed paths, {row['diff_bytes'] if row['diff_bytes'] is not None else '?'} diff bytes")
        if row["validations"]:
            parts.append("validations: " + ", ".join(f"{v['name']} {v['outcome']}" for v in row["validations"]))
        if row["deploy_none"]:
            parts.append(row["deploy"])
        self.detail.setText("\n".join(parts))
        diff = row["diff"]
        if row["access"] == "read":
            note, body = "Read-only repository: no diff (the agent saw the pinned baseline).", ""
        elif row["state"] == "unchanged":
            note, body = "No changes in this repository.", ""
        elif diff is None:
            note, body = "Diff not available from the daemon.", ""
        else:
            note = f"Diff shown: {len(diff['text'])} characters (agent output, shown as plain text)"
            if diff["truncated"]:
                note += "; TRUNCATED (the full diff is not shown)"
            elif diff.get("verified"):
                note += "; matches the manifest's diff_sha256"
            else:
                note += "; could not be matched to the manifest's diff_sha256"
            body = diff["text"]
        self.diff_note.setText(note)
        if self.diff.toPlainText() != body:
            self.diff.setPlainText(body)
