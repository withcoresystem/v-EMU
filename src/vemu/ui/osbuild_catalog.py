"""OSBuild catalog dialog.

Lists the distro/architecture/image-type combinations that the installed
``image-builder`` can build.  The dialog is read-only and never starts a build.
"""

from __future__ import annotations

import platform
import queue
import threading
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..errors import VemuError
from .delegate import NoFocusDelegate
from .dialogs import VemuDialog, dialog_layout
from .tooltips import set_tooltip
from .widgets import ComboBox


VEMU_IMAGE_TYPES = {
    "generic-qcow2",
    "qcow2",
    "minimal-installer",
    "image-installer",
    "workstation-live-installer",
    "server-qcow2",
    "server-installer",
    "live-installer",
}


def is_vemu_image_type(image_type: str) -> bool:
    """Whether an image-builder type is useful for a v-EMU lab build."""
    value = image_type.strip().lower()
    if value in VEMU_IMAGE_TYPES:
        return True
    return value.endswith("-qcow2") and not value.startswith("edge")


class OSBuildCatalogDialog(VemuDialog):
    """Browse the host image-builder catalog with a text/arch filter."""

    def __init__(self, adapter, parent=None) -> None:
        super().__init__(parent)
        self.adapter = adapter
        self.entries: list[dict[str, str]] = []
        self._loading = False
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)
        self.setWindowTitle("OSBuild catalog")
        self.resize(780, 600)
        self.setMinimumSize(640, 480)

        layout = dialog_layout(self)
        title = QLabel("OSBuild catalog")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Buildable combinations reported by image-builder on this host. "
            "By default only qcow2 and installer types that v-EMU can use are "
            "shown. Read-only: choose a blueprint and start a build from the "
            "Images tab."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        filters = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by distro or image type…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self._populate)
        filters.addWidget(self.search_edit, 1)
        self.arch_combo = ComboBox()
        self.arch_combo.setMinimumWidth(150)
        self.arch_combo.currentIndexChanged.connect(self._populate)
        filters.addWidget(self.arch_combo)
        self.all_types_check = QCheckBox("Show all image types")
        set_tooltip(
            self.all_types_check,
            "Include cloud, edge, container and other image types that v-EMU "
            "does not use.",
        )
        self.all_types_check.toggled.connect(self._populate)
        filters.addWidget(self.all_types_check)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("SecondaryButton")
        set_tooltip(
            self.refresh_button,
            "Run image-builder list again and refresh this catalog.",
        )
        self.refresh_button.clicked.connect(
            lambda: self.refresh(force=True)
        )
        filters.addWidget(self.refresh_button)
        layout.addLayout(filters)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            ["DISTRO", "ARCH", "IMAGE TYPE"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.setItemDelegate(NoFocusDelegate(self.table))
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, 110)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.setSortingEnabled(True)
        layout.addWidget(self.table, 1)

        self.summary = QLabel()
        self.summary.setObjectName("Muted")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("SecondaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self.refresh()

    # ------------------------------------------------------------------ data

    def refresh(self, *, force: bool = False) -> None:
        if self._loading:
            return
        self._loading = True
        self.entries = []
        self.table.setRowCount(0)
        self.search_edit.setEnabled(False)
        self.arch_combo.setEnabled(False)
        self.refresh_button.setEnabled(False)
        self.summary.setText(
            "Loading catalog from image-builder… this can take a few seconds."
        )
        self._queue = queue.Queue()

        def work() -> None:
            try:
                self._queue.put(
                    ("ok", list(self.adapter.catalog(refresh=force)))
                )
            except Exception as exc:  # noqa: BLE001 - show adapter errors
                self._queue.put(("error", str(exc)))

        threading.Thread(target=work, daemon=True).start()
        self._timer.start()

    def _poll(self) -> None:
        try:
            kind, payload = self._queue.get_nowait()
        except queue.Empty:
            return
        self._timer.stop()
        self._loading = False
        self.search_edit.setEnabled(True)
        self.arch_combo.setEnabled(True)
        self.refresh_button.setEnabled(True)
        if kind == "error":
            self.entries = []
            self.table.setRowCount(0)
            self.arch_combo.blockSignals(True)
            self.arch_combo.clear()
            self.arch_combo.addItem("All architectures", "")
            self.arch_combo.blockSignals(False)
            self.summary.setText(str(payload))
            return
        self.entries = list(payload)
        self._apply_entries()

    def _apply_entries(self) -> None:
        arches = sorted({entry["arch"] for entry in self.entries})
        selected = self.arch_combo.currentData()
        host_arch = platform.machine()
        self.arch_combo.blockSignals(True)
        self.arch_combo.clear()
        self.arch_combo.addItem("All architectures", "")
        for arch in arches:
            self.arch_combo.addItem(arch, arch)
        index = self.arch_combo.findData(selected) if selected else -1
        if index < 0 and host_arch in arches:
            index = self.arch_combo.findData(host_arch)
        self.arch_combo.setCurrentIndex(max(0, index))
        self.arch_combo.blockSignals(False)
        self._populate()

    def _populate(self, *_args: Any) -> None:
        query = self.search_edit.text().strip().lower()
        arch = str(self.arch_combo.currentData() or "")
        show_all = self.all_types_check.isChecked()
        rows = [
            entry
            for entry in self.entries
            if (not arch or entry["arch"] == arch)
            and (show_all or is_vemu_image_type(entry["image_type"]))
            and (
                not query
                or query in entry["distro"].lower()
                or query in entry["image_type"].lower()
                or query in entry["arch"].lower()
            )
        ]
        rows.sort(
            key=lambda entry: (
                entry["distro"],
                entry["image_type"],
                entry["arch"],
            )
        )
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for row, entry in enumerate(rows):
            for column, key in enumerate(("distro", "arch", "image_type")):
                item = QTableWidgetItem(entry[key])
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)
        self.table.sortItems(0, Qt.SortOrder.AscendingOrder)
        if not self.entries:
            self.summary.setText(
                "No build types were reported. Check that image-builder is "
                "installed and supported on this host."
            )
        elif not rows:
            self.summary.setText(
                f"No build types match the filter ({len(self.entries)} "
                "combinations available)."
            )
        else:
            scope = (
                "all image types"
                if self.all_types_check.isChecked()
                else "vEMU-relevant image types"
            )
            self.summary.setText(
                f"{len(rows)} of {len(self.entries)} build combinations "
                f"({scope})"
            )


__all__ = ["OSBuildCatalogDialog", "VEMU_IMAGE_TYPES", "is_vemu_image_type"]
