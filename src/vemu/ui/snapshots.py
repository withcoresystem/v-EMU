"""Snapshot manager for a v-EMU managed VM."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..errors import VemuError
from .delegate import NoFocusDelegate
from .dialogs import VemuDialog, dialog_layout
from .components import form_layout


_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def _created_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "—"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:10]
    return f"{parsed.day:02d}-{_MONTHS[parsed.month - 1]}-{parsed.year:04d}"


def _human_bytes(value: int | float) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


class _CompactTask(QThread):
    """Run a compaction plan or apply call off the GUI thread."""

    done = Signal(object, object)

    def __init__(
        self,
        backend,
        domain: str,
        *,
        dry_run: bool,
        confirm: bool,
    ) -> None:
        super().__init__()
        self._backend = backend
        self._domain = domain
        self._dry_run = dry_run
        self._confirm = confirm

    def run(self) -> None:
        try:
            payload = self._backend.snapshots_compact(
                self._domain,
                dry_run=self._dry_run,
                confirm=self._confirm,
            )
        except Exception as exc:  # noqa: BLE001 - forwarded to the dialog
            self.done.emit(None, exc)
        else:
            self.done.emit(payload, None)


class SnapshotEditDialog(VemuDialog):
    """Create or edit the user-facing name/description of a snapshot."""

    def __init__(
        self,
        parent=None,
        *,
        title: str = "New snapshot",
        name: str = "",
        description: str = "",
        editing: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 380)
        layout = dialog_layout(self)

        heading = QLabel(title)
        heading.setObjectName("DialogTitle")
        layout.addWidget(heading)

        form = form_layout()
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("e.g. before-upgrade")
        form.addRow("Name", self.name_edit)
        layout.addLayout(form)

        description_label = QLabel("Description")
        description_label.setObjectName("Muted")
        layout.addWidget(description_label)
        self.description_edit = QPlainTextEdit(description)
        self.description_edit.setPlaceholderText(
            "Optional: what changed, why this snapshot exists, rollback notes…"
        )
        layout.addWidget(self.description_edit, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.save_button = QPushButton("Save" if editing else "Create")
        self.save_button.setObjectName("PrimaryButton")
        self.save_button.clicked.connect(self.accept)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    def values(self) -> tuple[str, str]:
        return (
            self.name_edit.text().strip(),
            self.description_edit.toPlainText().strip(),
        )


class SnapshotsDialog(VemuDialog):
    def __init__(self, backend, domain_name: str, parent=None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.domain_name = domain_name
        self.info: dict[str, Any] = {}
        self.layers: dict[str, dict[str, Any]] = {}
        self.changed = False
        self._compact_task: _CompactTask | None = None
        self._compact_mode = "plan"

        self.setWindowTitle(f"Snapshots · {domain_name}")
        self.resize(680, 570)

        layout = dialog_layout(self)
        title = QLabel(f"Snapshots · {domain_name}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        self.state_label = QLabel()
        self.state_label.setObjectName("Muted")
        self.state_label.setWordWrap(True)
        layout.addWidget(self.state_label)

        self.compact_progress = QProgressBar()
        self.compact_progress.setRange(0, 0)
        self.compact_progress.setFixedHeight(4)
        self.compact_progress.setTextVisible(False)
        self.compact_progress.hide()
        layout.addWidget(self.compact_progress)

        self.table = QTreeWidget()
        self.table.setColumnCount(5)
        self.table.setHeaderLabels(
            ["ID", "NAME", "PARENT", "SIZE", "CREATED AT"]
        )
        self.table.setRootIsDecorated(True)
        # The tree column reserves one indentation step for the branch
        # indicator plus one for the item text. Match that in the header so
        # "ID"/"NAME" lines up with root-level rows.
        self.table.setIndentation(12)
        self.table.header().setStyleSheet(
            "QHeaderView::section:first { padding-left: 24px; }"
        )
        self.table.setUniformRowHeights(True)
        for column, alignment in enumerate(
            (
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
                Qt.AlignmentFlag.AlignLeft,
            )
        ):
            header_item = self.table.headerItem()
            if header_item is not None:
                header_item.setTextAlignment(
                    column, alignment | Qt.AlignmentFlag.AlignVCenter
                )
        self.table.setItemDelegate(NoFocusDelegate(self.table))
        self.table.header().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.table.header().setFixedHeight(30)
        self.table.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        self.table.header().resizeSection(0, 120)
        self.table.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Fixed
        )
        self.table.header().resizeSection(2, 90)
        self.table.header().setSectionResizeMode(
            3, QHeaderView.ResizeMode.Fixed
        )
        self.table.header().resizeSection(3, 100)
        self.table.header().setSectionResizeMode(
            4, QHeaderView.ResizeMode.Fixed
        )
        self.table.header().resizeSection(4, 130)
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.table, 1)

        details = QGroupBox("Description")
        details.setMinimumHeight(190)
        details_layout = QVBoxLayout(details)
        details_layout.setSpacing(6)
        self.details_description = QPlainTextEdit()
        self.details_description.setReadOnly(True)
        self.details_description.setPlaceholderText("No description.")
        self.details_description.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        details_layout.addWidget(self.details_description)
        layout.addWidget(details)

        buttons = QHBoxLayout()
        self.create_button = QPushButton("New snapshot")
        self.create_button.setObjectName("PrimaryButton")
        self.create_button.clicked.connect(self._create)
        buttons.addWidget(self.create_button)

        self.edit_button = QPushButton("Edit")
        self.edit_button.setObjectName("SecondaryButton")
        self.edit_button.clicked.connect(self._edit)
        buttons.addWidget(self.edit_button)

        self.revert_button = QPushButton("Revert")
        self.revert_button.setObjectName("SecondaryButton")
        self.revert_button.clicked.connect(self._revert)
        buttons.addWidget(self.revert_button)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("SecondaryButton")
        self.delete_button.clicked.connect(self._delete)
        buttons.addWidget(self.delete_button)

        self.compact_button = QPushButton("Compact…")
        self.compact_button.setObjectName("SecondaryButton")
        self.compact_button.setToolTip(
            "Requires at least two chain layers (snapshot + Current)."
        )
        self.compact_button.clicked.connect(self._compact)
        buttons.addWidget(self.compact_button)

        buttons.addStretch(1)
        self.close_button = QPushButton("Close")
        self.close_button.setObjectName("SecondaryButton")
        self.close_button.clicked.connect(self.accept)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

        self.refresh()

    # ------------------------------------------------------------------ data

    def refresh(self) -> None:
        try:
            self.info = self.backend.snapshots_info(self.domain_name)
        except VemuError as exc:
            self.state_label.setText(str(exc))
            self.table.clear()
            self._set_actions(False)
            return

        if not self.info.get("managed"):
            self._show_external()
            return

        self._set_headers(
            "ID", "NAME", "PARENT", "SIZE", "CREATED AT"
        )
        running = bool(self.info.get("running"))
        if running:
            text = (
                "The VM is running. Live snapshots are disabled; shut it down "
                "to create an offline snapshot."
            )
        else:
            text = f"Chain: {self.info.get('chain', '')} · VM is stopped."
        if self.info.get("multi_disk"):
            count = int(self.info.get("disk_count", 0))
            if count == 1:
                text += " Single-disk v2 chain."
            else:
                text += (
                    f" Multi-disk chain ({count} disks); "
                    "snapshots apply to every disk."
                )
        if self._model() >= 2:
            text += (
                " Snapshots freeze the current layer; the VM continues on a "
                "new current layer."
            )
        else:
            text += " Legacy snapshot layout: labels follow the created layer."
        self.state_label.setText(text)

        self.table.clear()
        layers = list(self.info.get("layers", []))
        self.layers = {
            str(layer.get("id", "")): layer for layer in layers
        }
        by_parent: dict[str, list[dict[str, Any]]] = {}
        for layer in layers:
            by_parent.setdefault(str(layer.get("parent", "")), []).append(layer)
        ids = {str(layer.get("id", "")) for layer in layers}

        def add_layer(
            layer: dict[str, Any], parent_item: QTreeWidgetItem | None = None
        ) -> None:
            layer_id = str(layer.get("id", ""))
            active = bool(layer.get("active"))
            size = layer.get("actual_size", 0) / (1024 * 1024)
            label = str(layer.get("label", "") or "")
            if self._model() >= 2 and active:
                label = "Current"
            elif not label:
                label = "-"
            item = QTreeWidgetItem(
                [
                    ("* " if active else "  ") + layer_id,
                    label,
                    str(layer.get("parent", "")),
                    f"{size:.1f} MB",
                    _created_text(layer.get("created_at")),
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, layer_id)
            if active:
                font = item.font(0)
                font.setBold(True)
                for column in range(5):
                    item.setFont(column, font)
            if parent_item is None:
                self.table.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
            for child in by_parent.get(layer_id, []):
                add_layer(child, item)

        roots = [layer for layer in layers if str(layer.get("parent")) == "base"]
        if not roots:
            roots = [
                layer
                for layer in layers
                if str(layer.get("parent")) not in ids
            ]
        for layer in roots:
            add_layer(layer)
        self.table.expandAll()

        self.create_button.setText("New snapshot")
        self.create_button.setEnabled(not running)
        self.edit_button.setEnabled(False)
        self.revert_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.compact_button.setEnabled(self._can_compact())
        self._show_details()

    def _set_headers(self, *labels: str) -> None:
        self.table.setColumnCount(len(labels))
        self.table.setHeaderLabels(list(labels))
        header = self.table.header()
        for column in range(len(labels)):
            item = self.table.headerItem()
            if item is not None:
                item.setTextAlignment(
                    column,
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter,
                )
        if labels and labels[0] == "NAME":
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(1, 90)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(2, 90)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
            return
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(0, 120)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(2, 90)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(3, 100)
        if len(labels) > 4:
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
            header.resizeSection(4, 130)

    def _show_external(self) -> None:
        snapshots = list(self.info.get("snapshots", []))
        self._set_headers("NAME", "TYPE", "DISKS", "SOURCE")
        reason = self.info.get("reason", "This VM is not managed by v-EMU.")
        if snapshots:
            self.state_label.setText(
                f"{reason}\nLibvirt/Cockpit snapshots are shown read-only; "
                "Full migration does not import them."
            )
        else:
            self.state_label.setText(
                f"{reason}\nNo libvirt/Cockpit snapshots found."
            )
        self.table.clear()
        self.layers = {}
        for snapshot in snapshots:
            name = str(snapshot.get("name", ""))
            disks = list(snapshot.get("disks", []))
            item = QTreeWidgetItem(
                [
                    name,
                    str(snapshot.get("kind", "")),
                    str(len(disks)),
                    "",
                ]
            )
            for disk in disks:
                item.addChild(
                    QTreeWidgetItem(
                        [
                            str(disk.get("target", "")),
                            str(disk.get("snapshot", "")),
                            "",
                            str(disk.get("source", "")),
                        ]
                    )
                )
            self.table.addTopLevelItem(item)
        self.table.expandAll()
        self._set_actions(False)
        self._show_details()

    def _model(self) -> int:
        return int(self.info.get("snapshot_model") or 1)

    def _is_current(self, layer_id: str | None) -> bool:
        return (
            self._model() >= 2
            and layer_id is not None
            and layer_id == self.info.get("active")
        )

    def _can_compact(self) -> bool:
        if not bool(self.info.get("managed")):
            return False
        if bool(self.info.get("running")):
            return False
        return len(self.layers) >= 2

    def _selection_changed(self) -> None:
        running = bool(self.info.get("running"))
        selected = self._selected_layer() is not None
        managed = bool(self.info.get("managed"))
        current = self._is_current(self._selected_layer())
        self.edit_button.setEnabled(selected and managed and not current)
        self.revert_button.setEnabled(
            selected and not running and managed and not current
        )
        self.delete_button.setEnabled(
            selected and not running and managed and not current
        )
        self._show_details()

    def _set_actions(self, enabled: bool) -> None:
        self.create_button.setEnabled(enabled)
        self.edit_button.setEnabled(False)
        self.revert_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.compact_button.setEnabled(False)

    def _show_details(self) -> None:
        managed = bool(self.info.get("managed"))
        if not managed:
            item = self.table.currentItem()
            while item is not None and item.parent() is not None:
                item = item.parent()
            if item is None:
                self.details_description.setPlainText("")
                return
            name = item.text(0)
            snapshot = next(
                (
                    entry
                    for entry in self.info.get("snapshots", [])
                    if str(entry.get("name", "")) == name
                ),
                None,
            )
            description = (
                str(snapshot.get("description", ""))
                if snapshot is not None
                else ""
            )
            self.details_description.setPlainText(
                description or "No description."
            )
            return

        layer_id = self._selected_layer()
        layer = self.layers.get(layer_id or "")
        if layer is None:
            self.details_description.setPlainText("")
            return
        if self._is_current(layer_id):
            self.details_description.setPlainText(
                "Current state. It is not a snapshot: create a snapshot to "
                "preserve it, or revert to an existing snapshot to discard it."
            )
            return
        self.details_description.setPlainText(
            str(layer.get("description", "")) or "No description."
        )

    def _selected_layer(self) -> str | None:
        selected = self.table.selectedItems()
        if not selected:
            return None
        value = selected[0].data(0, Qt.ItemDataRole.UserRole)
        return str(value) if value is not None else None

    # ------------------------------------------------------------------ actions

    def _build_context_menu(self) -> QMenu:
        menu = QMenu(self)
        create = menu.addAction(self.create_button.text(), self._create)
        create.setEnabled(self.create_button.isEnabled())
        menu.addSeparator()
        layer = self._selected_layer()
        current = self._is_current(layer)
        edit = menu.addAction("Edit snapshot…", self._edit)
        edit.setEnabled(
            layer is not None
            and bool(self.info.get("managed"))
            and not current
        )
        can_modify = (
            layer is not None
            and not bool(self.info.get("running"))
            and not current
        )
        revert = menu.addAction("Revert", self._revert)
        revert.setEnabled(can_modify)
        delete = menu.addAction("Delete", self._delete)
        delete.setEnabled(can_modify)
        menu.addSeparator()
        compact = menu.addAction("Compact chain…", self._compact)
        compact.setEnabled(self._can_compact())
        menu.addSeparator()
        copy = menu.addAction(
            "Copy ID",
            lambda: QGuiApplication.clipboard().setText(layer or ""),
        )
        copy.setEnabled(layer is not None)
        return menu

    def _show_context_menu(self, position) -> None:
        item = self.table.itemAt(position)
        if item is None:
            return
        self.table.setCurrentItem(item)
        self._build_context_menu().exec(
            self.table.viewport().mapToGlobal(position)
        )

    def _create(self) -> None:
        dialog = SnapshotEditDialog(
            self, title="New snapshot", editing=False
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        label, description = dialog.values()
        if self._model() >= 2 and not label:
            QMessageBox.warning(
                self,
                "Snapshot name required",
                "Give the snapshot a name so it can be identified in the "
                "tree.",
            )
            return
        try:
            payload = self.backend.snapshots_create(
                self.domain_name, label, description
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Snapshot failed", str(exc))
            return
        warnings = (
            payload.get("warnings", []) if isinstance(payload, dict) else []
        )
        if warnings:
            QMessageBox.warning(
                self, "Snapshot warning", "\n".join(str(item) for item in warnings)
            )
        self._show_ejected(payload)
        self.changed = True
        self.refresh()

    def _edit(self) -> None:
        layer = self._selected_layer()
        if layer is None or not bool(self.info.get("managed")):
            return
        data = self.layers.get(layer, {})
        dialog = SnapshotEditDialog(
            self,
            title="Edit snapshot",
            name=str(data.get("label", "")),
            description=str(data.get("description", "")),
            editing=True,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        label, description = dialog.values()
        try:
            self.backend.snapshots_update(
                self.domain_name,
                layer,
                label=label,
                description=description,
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Snapshot update failed", str(exc))
            return
        self.changed = True
        self.refresh()

    def _show_ejected(self, payload: object) -> None:
        ejected = (
            payload.get("ejected", []) if isinstance(payload, dict) else []
        )
        if not ejected:
            return
        QMessageBox.information(
            self,
            "CD/DVD ejected",
            "Ejected media: "
            + ", ".join(str(item) for item in ejected)
            + "\n\nRe-insert it from the Hardware editor if needed.",
        )

    def _revert(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            return
        data = self.layers.get(layer, {})
        label = str(data.get("label", "") or "").strip()
        name = f"{label} ({layer})" if label else f"layer {layer}"
        model = (
            "The current layer and every change since this snapshot will be "
            "discarded."
            if self._model() >= 2
            else "Changes after this snapshot will be lost."
        )
        answer = QMessageBox.question(
            self,
            "Revert snapshot",
            f"Revert to {name}? {model}\n\n"
            "CD/DVD media will be ejected first.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            payload = self.backend.snapshots_revert(self.domain_name, layer)
        except VemuError as exc:
            QMessageBox.critical(self, "Revert failed", str(exc))
            return
        self._show_ejected(payload)
        self.changed = True
        self.refresh()

    def _delete(self) -> None:
        layer = self._selected_layer()
        if layer is None:
            return
        is_active = layer == self.info.get("active")
        data = self.layers.get(layer, {})
        label = str(data.get("label", "") or "").strip()
        name = f"{label} ({layer})" if label else f"layer {layer}"
        if self._model() >= 2:
            question = (
                f"Delete snapshot {name}? The current state is kept; this "
                "only removes the rollback point."
            )
        else:
            extra = (
                " This is the active layer; current state will be discarded."
                if is_active
                else ""
            )
            question = f"Delete layer {layer}?{extra}"
        answer = QMessageBox.question(
            self,
            "Delete snapshot",
            question,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.backend.snapshots_delete(
                self.domain_name, layer, force=is_active
            )
        except VemuError as exc:
            QMessageBox.critical(self, "Delete failed", str(exc))
            return
        self.changed = True
        self.refresh()

    # ------------------------------------------------------------------ compaction

    def closeEvent(self, event) -> None:
        if self._compact_task is not None:
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        if self._compact_task is not None:
            return
        super().reject()

    def _compact(self) -> None:
        if self._compact_task is not None or not self._can_compact():
            return
        self._start_compact(dry_run=True, confirm=False)

    def _start_compact(self, *, dry_run: bool, confirm: bool) -> None:
        self._compact_mode = "plan" if dry_run else "apply"
        self.compact_button.setEnabled(False)
        self.create_button.setEnabled(False)
        self.edit_button.setEnabled(False)
        self.revert_button.setEnabled(False)
        self.delete_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.state_label.setText(
            "Measuring the chain and free space…"
            if dry_run
            else "Compacting the chain; this can take a while…"
        )
        self.compact_progress.show()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self._compact_task = _CompactTask(
            self.backend,
            self.domain_name,
            dry_run=dry_run,
            confirm=confirm,
        )
        self._compact_task.done.connect(self._compact_done)
        self._compact_task.finished.connect(self._compact_finished)
        self._compact_task.start()

    def _compact_done(self, payload, error) -> None:
        if error is not None:
            QMessageBox.critical(self, "Compaction failed", str(error))
            return
        if self._compact_mode == "plan":
            self._confirm_compact(payload)
            return
        self.changed = True
        self.refresh()
        reclaim = int(payload.get("reclaim_bytes", 0) or 0)
        host_reclaim = payload.get("host_reclaim_bytes")
        if isinstance(host_reclaim, int) and host_reclaim > 0:
            reclaim = host_reclaim
        reclaimed = (
            f"Reclaimed about {_human_bytes(reclaim)}."
            if reclaim > 0
            else "Chain compacted; disk usage may not shrink."
        )
        QMessageBox.information(
            self,
            "Compaction complete",
            f"{reclaimed} Active layer: {payload.get('active', '')}.",
        )
        warnings = payload.get("warnings") or []
        if warnings:
            QMessageBox.warning(
                self,
                "Compaction warnings",
                "\n".join(str(item) for item in warnings),
            )

    def _confirm_compact(self, plan: dict) -> None:
        if not plan.get("supported"):
            reasons = plan.get("reasons") or [
                "Compaction is not available for this chain."
            ]
            QMessageBox.warning(
                self,
                "Cannot compact",
                "\n".join(f"- {item}" for item in reasons),
            )
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Compact chain")
        box.setText(f"Compact the snapshot chain for {self.domain_name}?")
        reclaim = int(plan.get("reclaim_bytes", 0) or 0)
        reclaim_text = (
            _human_bytes(reclaim)
            if reclaim > 0
            else "none (may need extra space)"
        )
        box.setInformativeText(
            f"Layers: {plan.get('layer_count', 0)} "
            f"({plan.get('snapshot_count', 0)} snapshot(s))\n"
            f"Estimated reclaim: {reclaim_text}\n"
            f"Required free space: "
            f"{_human_bytes(plan.get('required_bytes', 0))} "
            f"(available {_human_bytes(plan.get('available_bytes', 0))})\n\n"
            "This merges the active branch into the first layer, discards "
            "the other layers and removes all snapshot rollback points."
        )
        warnings = plan.get("warnings") or []
        if warnings:
            box.setDetailedText(
                "\n".join(str(item) for item in warnings)
            )
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        if box.exec() != QMessageBox.StandardButton.Yes:
            return
        self._start_compact(dry_run=False, confirm=True)

    def _compact_finished(self) -> None:
        QApplication.restoreOverrideCursor()
        self.compact_progress.hide()
        if self._compact_task is not None:
            self._compact_task.deleteLater()
            self._compact_task = None
        self.close_button.setEnabled(True)
        self.compact_button.setEnabled(self._can_compact())
        self.create_button.setEnabled(
            bool(self.info.get("managed"))
            and not bool(self.info.get("running"))
        )
        self._selection_changed()
