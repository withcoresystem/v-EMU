"""Package and group lookup for Image Builder targets."""

from __future__ import annotations

import platform
import queue
import re
import shutil
import threading
from typing import Any, Callable

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..image_metadata import (
    ImageMetadataStore,
    SolverLookup,
    repos_for_target,
    solver_cache_dir,
    target_key,
)
from .delegate import NoFocusDelegate
from .dialogs import VemuDialog, dialog_layout
from .tooltips import set_tooltip
from .widgets import ComboBox


class ImagePackageLookupDialog(VemuDialog):
    """Search packages and groups using the target distro repositories."""

    def __init__(self, adapter, parent=None, blueprint=None) -> None:
        super().__init__(parent)
        self.adapter = adapter
        self.blueprint = blueprint
        self.store = getattr(adapter, "_metadata", None) or ImageMetadataStore()
        self.entries: list[dict[str, str]] = []
        self._groups: list[dict[str, str]] = []
        self._groups_loaded = False
        self._busy = False
        self._queue: queue.Queue[tuple[str, str, Any]] = queue.Queue()
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)

        self.setWindowTitle("Package lookup")
        self.resize(900, 620)
        self.setMinimumSize(720, 500)

        layout = dialog_layout(self)
        title = QLabel("Package and group lookup")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Search the package repositories for the selected target. This is "
            "read-only: nothing is installed or built. The first query prepares "
            "repository metadata; later queries use the local cache."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)

        target_row = QHBoxLayout()
        target_row.addWidget(QLabel("Distro"))
        self.distro_combo = ComboBox()
        self.distro_combo.setMinimumWidth(190)
        self.distro_combo.setEditable(True)
        self.distro_combo.currentTextChanged.connect(self._distro_changed)
        target_row.addWidget(self.distro_combo)
        target_row.addWidget(QLabel("Arch"))
        self.arch_combo = ComboBox()
        self.arch_combo.setMinimumWidth(110)
        target_row.addWidget(self.arch_combo)
        target_row.addWidget(QLabel("Extra repos"))
        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText(
            "Optional comma-separated URLs (CRB, EPEL, …)"
        )
        set_tooltip(
            self.extra_edit,
            "Additional repositories used only for this lookup. They are not "
            "saved into the blueprint.",
        )
        target_row.addWidget(self.extra_edit, 1)
        layout.addLayout(target_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_packages_tab(), "Packages")
        self.tabs.addTab(self._build_groups_tab(), "Groups")
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs, 1)

        self.status = QLabel()
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh metadata")
        self.refresh_button.setObjectName("SecondaryButton")
        set_tooltip(
            self.refresh_button,
            "Discard cached repository and query data for this target and "
            "reload it.",
        )
        self.refresh_button.clicked.connect(self._refresh_metadata)
        buttons.addWidget(self.refresh_button)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("SecondaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self._load_targets()

    # ------------------------------------------------------------------ setup

    def _build_packages_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Package names or globs, comma-separated, for example inkscape, *kde*"
        )
        set_tooltip(
            self.search_edit,
            "Separate multiple package names or globs with commas.",
        )
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.returnPressed.connect(self._search_packages)
        row.addWidget(self.search_edit, 1)
        self.search_button = QPushButton("Search")
        self.search_button.setObjectName("PrimaryButton")
        self.search_button.clicked.connect(self._search_packages)
        row.addWidget(self.search_button)
        layout.addLayout(row)
        self.packages_table = self._table(
            ["PACKAGE", "VERSION", "ARCH", "REPO", "SUMMARY"],
            {1: 150, 2: 90, 3: 110},
        )
        self.packages_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.packages_table.customContextMenuRequested.connect(
            self._packages_menu
        )
        self.packages_table.setToolTip(
            "Right-click a row to copy the package name, version or row."
        )
        layout.addWidget(self.packages_table, 1)
        return page

    def _build_groups_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        row = QHBoxLayout()
        self.group_filter = QLineEdit()
        self.group_filter.setPlaceholderText(
            "Filter package groups (comma-separated)…"
        )
        set_tooltip(
            self.group_filter,
            "Separate multiple group names with commas.",
        )
        self.group_filter.setClearButtonEnabled(True)
        self.group_filter.textChanged.connect(self._populate_groups)
        row.addWidget(self.group_filter, 1)
        self.groups_button = QPushButton("Load groups")
        self.groups_button.setObjectName("SecondaryButton")
        self.groups_button.clicked.connect(self._load_groups)
        row.addWidget(self.groups_button)
        self.hidden_check = QCheckBox("Show hidden groups")
        set_tooltip(
            self.hidden_check,
            "Include groups marked hidden by the distribution. Fedora shows "
            "many more groups with this option.",
        )
        self.hidden_check.toggled.connect(self._hidden_groups_changed)
        row.addWidget(self.hidden_check)
        layout.addLayout(row)
        self.groups_table = self._table(["GROUP", "NAME"], {0: 220})
        self.groups_table.itemSelectionChanged.connect(
            self._group_selection_changed
        )
        self.groups_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.groups_table.customContextMenuRequested.connect(
            self._groups_menu
        )
        self.groups_table.setToolTip(
            "Right-click a row to copy the group ID, name or row."
        )
        layout.addWidget(self.groups_table, 1)
        row = QHBoxLayout()
        self.validate_button = QPushButton("Validate selected group")
        self.validate_button.setObjectName("SecondaryButton")
        self.validate_button.setEnabled(False)
        self.validate_button.clicked.connect(self._validate_group)
        row.addWidget(self.validate_button)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def _table(
        self, headers: list[str], widths: dict[int, int]
    ) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        table.setItemDelegate(NoFocusDelegate(table))
        table.setSortingEnabled(False)
        header = table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        for column, width in widths.items():
            header.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Fixed
            )
            header.resizeSection(column, width)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        return table

    @staticmethod
    def _cell_text(table: QTableWidget, row: int, column: int) -> str:
        item = table.item(row, column)
        return item.text() if item is not None else ""

    def _row_text(self, table: QTableWidget, row: int) -> str:
        values = [
            self._cell_text(table, row, column)
            for column in range(table.columnCount())
        ]
        return "\t".join(value for value in values if value)

    def _copy(self, text: str, label: str) -> None:
        QApplication.clipboard().setText(text)
        self.status.setText(f"Copied {label}: {text}")

    def _packages_menu(self, position) -> None:
        row = self.packages_table.rowAt(position.y())
        if row < 0:
            return
        self.packages_table.selectRow(row)
        menu = QMenu(self)
        copy_name = menu.addAction("Copy package name")
        copy_version = menu.addAction("Copy version")
        copy_row = menu.addAction("Copy row")
        chosen = menu.exec(
            self.packages_table.viewport().mapToGlobal(position)
        )
        if chosen is copy_name:
            self._copy(
                self._cell_text(self.packages_table, row, 0), "package"
            )
        elif chosen is copy_version:
            self._copy(
                self._cell_text(self.packages_table, row, 1), "version"
            )
        elif chosen is copy_row:
            self._copy(self._row_text(self.packages_table, row), "row")

    def _groups_menu(self, position) -> None:
        row = self.groups_table.rowAt(position.y())
        if row < 0:
            return
        self.groups_table.selectRow(row)
        menu = QMenu(self)
        copy_id = menu.addAction("Copy group ID")
        copy_name = menu.addAction("Copy group name")
        copy_row = menu.addAction("Copy row")
        chosen = menu.exec(self.groups_table.viewport().mapToGlobal(position))
        if chosen is copy_id:
            self._copy(self._cell_text(self.groups_table, row, 0), "group ID")
        elif chosen is copy_name:
            self._copy(
                self._cell_text(self.groups_table, row, 1), "group name"
            )
        elif chosen is copy_row:
            self._copy(self._row_text(self.groups_table, row), "row")

    # ----------------------------------------------------------------- targets

    def _load_targets(self) -> None:
        try:
            self.entries = list(self.adapter.catalog())
        except Exception as exc:  # noqa: BLE001 - show catalog errors
            self.status.setText(str(exc))
            self.distro_combo.setEnabled(False)
            self.arch_combo.setEnabled(False)
            self.search_button.setEnabled(False)
            self.groups_button.setEnabled(False)
            return
        distros = sorted({entry["distro"] for entry in self.entries})
        preferred = ""
        if self.blueprint is not None:
            preferred = str(self.blueprint.distro or "")
        if not preferred or preferred not in distros:
            try:
                host = platform.freedesktop_os_release()
            except OSError:
                host = {}
            host_id = str(host.get("ID", "")).lower()
            host_version = str(host.get("VERSION_ID", ""))
            candidate = (
                f"{host_id}-{host_version}"
                if host_id and host_version
                else ""
            )
            if candidate in distros:
                preferred = candidate
        if not preferred or preferred not in distros:
            preferred = next(
                (
                    name
                    for name in distros
                    if name.startswith("fedora")
                ),
                distros[0] if distros else "",
            )
        self.distro_combo.blockSignals(True)
        self.distro_combo.clear()
        for name in distros:
            self.distro_combo.addItem(name, name)
        self.distro_combo.blockSignals(False)
        index = self.distro_combo.findText(preferred)
        if index >= 0:
            self.distro_combo.setCurrentIndex(index)
        if self.blueprint is not None:
            try:
                repos = self.adapter.extra_repos(self.blueprint)
            except Exception:  # noqa: BLE001 - blueprint may be unreadable
                repos = []
            self.extra_edit.setText(", ".join(repos))
        self._distro_changed(preferred or self.distro_combo.currentText())

    def _distro_changed(self, distro: str) -> None:
        arches = sorted(
            {
                entry["arch"]
                for entry in self.entries
                if entry["distro"] == distro
            }
        )
        if not arches:
            arches = [platform.machine()]
        selected = self.arch_combo.currentData()
        host_arch = platform.machine()
        self.arch_combo.blockSignals(True)
        self.arch_combo.clear()
        for arch in arches:
            self.arch_combo.addItem(arch, arch)
        index = self.arch_combo.findData(selected) if selected else -1
        if index < 0 and host_arch in arches:
            index = self.arch_combo.findData(host_arch)
        self.arch_combo.setCurrentIndex(max(0, index))
        self.arch_combo.blockSignals(False)
        self._clear_results()

    def _clear_results(self) -> None:
        self.packages_table.setRowCount(0)
        self.groups_table.setRowCount(0)
        self._groups = []
        self._groups_loaded = False
        self.validate_button.setEnabled(False)

    def _extra_repos(self) -> list[str]:
        return [
            part
            for part in re.split(r"[\s,]+", self.extra_edit.text().strip())
            if part
        ]

    def _target(self) -> tuple[str, str, list[str]]:
        distro = self.distro_combo.currentText().strip()
        arch = str(self.arch_combo.currentData() or self.arch_combo.currentText())
        return distro, arch, self._extra_repos()

    # ---------------------------------------------------------------- workers

    def _lookup(
        self, distro: str, arch: str, extra: list[str]
    ) -> SolverLookup:
        key, repos = repos_for_target(
            self.store, distro, arch, extra_repos=extra
        )
        return SolverLookup(
            distro,
            arch,
            repos,
            store=self.store,
            target=key,
        )

    def _run(self, kind: str, work: Callable[[], Any]) -> None:
        if self._busy:
            return
        self._busy = True
        self._set_controls(False)
        self.status.setText(
            "Preparing repository metadata and querying the target… the "
            "first run can take a few seconds."
        )

        def run() -> None:
            try:
                self._queue.put((kind, "ok", work()))
            except Exception as exc:  # noqa: BLE001 - display lookup errors
                self._queue.put((kind, "error", str(exc)))

        threading.Thread(target=run, daemon=True).start()
        self._timer.start()

    def _poll(self) -> None:
        try:
            kind, state, payload = self._queue.get_nowait()
        except queue.Empty:
            return
        self._timer.stop()
        self._busy = False
        self._set_controls(True)
        if state == "error":
            self.status.setText(str(payload))
            return
        if kind == "packages":
            self._show_packages(payload)
        elif kind == "groups":
            self._groups = list(payload)
            self._groups_loaded = True
            self._populate_groups()
            scope = (
                " including hidden groups"
                if self.hidden_check.isChecked()
                else ""
            )
            self.status.setText(
                f"{len(self._groups)} package group(s) for the target"
                f"{scope}."
            )
        elif kind == "group":
            count = int(payload.get("count") or 0)
            group = str(payload.get("group") or "")
            names = ", ".join(payload.get("packages", [])[:8])
            suffix = "…" if payload.get("truncated") else ""
            self.status.setText(
                f"Group '{group}' resolves to {count} package(s)."
                + (f" {names}{suffix}" if names else "")
            )

    def _set_controls(self, enabled: bool) -> None:
        self.search_button.setEnabled(enabled)
        self.groups_button.setEnabled(enabled)
        self.hidden_check.setEnabled(enabled)
        self.refresh_button.setEnabled(enabled)
        self.search_edit.setEnabled(enabled)
        self.group_filter.setEnabled(enabled)
        self.distro_combo.setEnabled(enabled)
        self.arch_combo.setEnabled(enabled)
        self.extra_edit.setEnabled(enabled)
        self.validate_button.setEnabled(
            enabled and bool(self._groups) and self._selected_group() is not None
        )

    # --------------------------------------------------------------- packages

    def _search_packages(self) -> None:
        distro, arch, extra = self._target()
        query = self.search_edit.text().strip()
        if not distro or not query:
            self.status.setText("Enter a package name and select a target.")
            return
        self._run(
            "packages",
            lambda: self._lookup(distro, arch, extra).search_packages(query),
        )

    def _show_packages(self, rows: list[dict[str, str]]) -> None:
        self.packages_table.setRowCount(len(rows))
        for row, entry in enumerate(rows):
            values = (
                entry.get("name", ""),
                entry.get("version", ""),
                entry.get("arch", ""),
                entry.get("repo", ""),
                entry.get("summary", ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                self.packages_table.setItem(row, column, item)
        self.status.setText(
            f"{len(rows)} package(s) found for "
            f"'{self.search_edit.text().strip()}'."
            if rows
            else f"No packages found for '{self.search_edit.text().strip()}'."
        )

    # ----------------------------------------------------------------- groups

    def _tab_changed(self, index: int) -> None:
        if index == 1 and not self._groups_loaded and not self._busy:
            self._load_groups()

    def _load_groups(self) -> None:
        distro, arch, extra = self._target()
        if not distro:
            return
        include_hidden = self.hidden_check.isChecked()
        self._run(
            "groups",
            lambda: self._lookup(distro, arch, extra).group_list(
                include_hidden=include_hidden
            ),
        )

    def _hidden_groups_changed(self, _checked: bool) -> None:
        if not self._groups_loaded or self._busy:
            return
        self._groups = []
        self._groups_loaded = False
        self.groups_table.setRowCount(0)
        self.validate_button.setEnabled(False)
        self._load_groups()

    def _populate_groups(self) -> None:
        terms = [
            part.strip().lower()
            for part in self.group_filter.text().split(",")
            if part.strip()
        ]
        rows = [
            group
            for group in self._groups
            if not terms
            or any(
                term in group["id"].lower()
                or term in group["name"].lower()
                for term in terms
            )
        ]
        self.groups_table.setRowCount(len(rows))
        for row, group in enumerate(rows):
            for column, key in enumerate(("id", "name")):
                item = QTableWidgetItem(group[key])
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsSelectable
                )
                self.groups_table.setItem(row, column, item)
        self._group_selection_changed()

    def _selected_group(self) -> str | None:
        row = self.groups_table.currentRow()
        if row < 0:
            return None
        item = self.groups_table.item(row, 0)
        return item.text() if item is not None else None

    def _group_selection_changed(self) -> None:
        self.validate_button.setEnabled(
            not self._busy and self._selected_group() is not None
        )

    def _validate_group(self) -> None:
        group = self._selected_group()
        distro, arch, extra = self._target()
        if not group or not distro:
            return
        self._run(
            "group",
            lambda: self._lookup(distro, arch, extra).validate_group(group),
        )

    # ---------------------------------------------------------------- refresh

    def _refresh_metadata(self) -> None:
        distro, arch, extra = self._target()
        if not distro:
            return
        key = target_key(distro, arch, extra)
        self.store.clear_target(key)
        try:
            shutil.rmtree(solver_cache_dir(key), ignore_errors=True)
        except OSError:
            pass
        self._clear_results()
        if self.tabs.currentIndex() == 1:
            self._load_groups()
        else:
            self._search_packages()
        self.status.setText("Metadata cache cleared for this target.")


__all__ = ["ImagePackageLookupDialog"]
