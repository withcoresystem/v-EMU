"""Four-step Image Builder wizard: Mode, Blueprint, Preflight, Build."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..errors import VemuError
from ..iso_builder import IsoOptions
from ..imagebuilder import (
    BlueprintRecord,
    BuildJob,
    BuildMode,
    FakeImageBuilderAdapter,
    PreflightCheck,
    PreflightResult,
)
from .components import form_layout
from .dialogs import VemuDialog, dialog_layout
from .theme import DARK, LIGHT
from .widgets import ComboBox

STEP_TITLES = ("Mode", "Blueprint", "Preflight", "Build")


class PreflightWorker(QThread):
    """Run an adapter preflight off the UI thread."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        adapter,
        mode: BuildMode,
        blueprint: BlueprintRecord,
        output_dir: str,
        parent=None,
        iso_options: IsoOptions | None = None,
    ) -> None:
        super().__init__(parent)
        self.adapter = adapter
        self.mode = mode
        self.blueprint = blueprint
        self.output_dir = output_dir
        self.iso_options = iso_options
        self.cancel = threading.Event()

    def run(self) -> None:
        try:
            result = self.adapter.preflight(
                self.mode,
                self.blueprint,
                self.output_dir,
                cancel=self.cancel,
                iso_options=self.iso_options,
            )
        except Exception as exc:  # noqa: BLE001 - surface adapter errors
            if not self.cancel.is_set():
                self.failed.emit(str(exc))
            return
        if not self.cancel.is_set():
            self.done.emit(result)

    def stop(self) -> None:
        self.cancel.set()
        self.wait(4000)


class BuildWizardDialog(VemuDialog):
    def __init__(
        self,
        adapter=None,
        *,
        dark: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent, title="New image build")
        self.adapter = adapter or FakeImageBuilderAdapter()
        self.dark = dark
        self._tokens = DARK if dark else LIGHT
        self.mode: BuildMode = "qcow2"
        self.blueprints: list[BlueprintRecord] = []
        self.blueprint: BlueprintRecord | None = None
        self.preflight: PreflightResult | None = None
        self.created: BuildJob | None = None
        self._preflight_worker: PreflightWorker | None = None
        self._resolving_profile = False
        self._qemu_variant_ready = True
        self.step = 0
        self.resize(760, 600)

        layout = dialog_layout(self)
        title = QLabel("New image build")
        title.setObjectName("HeroTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "Choose a mode, load a blueprint, run preflight, then build."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        self.step_label = QLabel()
        self.step_label.setObjectName("Muted")
        layout.addWidget(self.step_label)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_mode_page())
        self.stack.addWidget(self._build_blueprint_page())
        self.stack.addWidget(self._build_preflight_page())
        self.stack.addWidget(self._build_build_page())
        layout.addWidget(self.stack, 1)

        buttons = QHBoxLayout()
        self.back_button = QPushButton("Back")
        self.back_button.setObjectName("SecondaryButton")
        self.back_button.clicked.connect(self._back)
        buttons.addWidget(self.back_button)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.next_button = QPushButton("Next")
        self.next_button.setObjectName("PrimaryButton")
        self.next_button.clicked.connect(self._next)
        buttons.addWidget(self.next_button)
        self.start_button = QPushButton("Start build")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.clicked.connect(self._start_build)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)

        self._mode_changed()
        self._refresh_blueprints()
        self._resolve_profile()
        self._sync_navigation()

    # ------------------------------------------------------------------ pages

    def _build_mode_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        form = form_layout()
        self.mode_combo = ComboBox()
        self.mode_combo.addItem("Image (qcow2) — lab image", "qcow2")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        form.addRow("Build mode", self.mode_combo)
        layout.addLayout(form)
        self.mode_note = QLabel()
        self.mode_note.setObjectName("Muted")
        self.mode_note.setWordWrap(True)
        layout.addWidget(self.mode_note)
        self.qemu_check = QCheckBox(
            "QEMU-optimized (remove firmware not used by virtual hardware)"
        )
        self.qemu_check.setToolTip(
            "Selects the blueprint-qemu.toml variant when available. "
            "Firmware packages are removed on first boot; physical "
            "passthrough and bare-metal hardware are not supported."
        )
        self.qemu_check.toggled.connect(self._profile_toggled)
        layout.addWidget(self.qemu_check)
        self.profile_note = QLabel()
        self.profile_note.setObjectName("Muted")
        self.profile_note.setWordWrap(True)
        layout.addWidget(self.profile_note)
        layout.addStretch(1)
        return page

    def _build_blueprint_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        row = QHBoxLayout()
        self.blueprint_combo = ComboBox()
        self.blueprint_combo.currentIndexChanged.connect(
            self._blueprint_changed
        )
        row.addWidget(self.blueprint_combo, 1)
        self.import_blueprint_button = QPushButton("Import blueprint…")
        self.import_blueprint_button.setObjectName("SecondaryButton")
        self.import_blueprint_button.clicked.connect(self._import_blueprint)
        row.addWidget(self.import_blueprint_button)
        layout.addLayout(row)

        form = form_layout()
        self.blueprint_name = QLabel("—")
        form.addRow("Name", self.blueprint_name)
        self.blueprint_version = QLabel("—")
        form.addRow("Version", self.blueprint_version)
        self.blueprint_distro = QLabel("—")
        form.addRow("Distro", self.blueprint_distro)
        self.blueprint_path = QLabel("—")
        self.blueprint_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.blueprint_path.setWordWrap(True)
        form.addRow("Path", self.blueprint_path)
        layout.addLayout(form)

        self.blueprint_note = QLabel()
        self.blueprint_note.setObjectName("Muted")
        self.blueprint_note.setWordWrap(True)
        layout.addWidget(self.blueprint_note)
        layout.addStretch(1)
        return page

    def _build_preflight_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.preflight_summary = QLabel()
        self.preflight_summary.setObjectName("Muted")
        self.preflight_summary.setWordWrap(True)
        layout.addWidget(self.preflight_summary)
        self.checks_box = QWidget()
        self.checks_layout = QVBoxLayout(self.checks_box)
        self.checks_layout.setContentsMargins(0, 0, 0, 0)
        self.checks_layout.setSpacing(8)
        layout.addWidget(self.checks_box)
        self.preflight_note = QLabel()
        self.preflight_note.setObjectName("Muted")
        self.preflight_note.setWordWrap(True)
        layout.addWidget(self.preflight_note)
        self.recheck_button = QPushButton("Recheck")
        self.recheck_button.setObjectName("SecondaryButton")
        self.recheck_button.clicked.connect(self._run_preflight)
        row = QHBoxLayout()
        row.addWidget(self.recheck_button)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def _build_build_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        form = form_layout()
        output_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.textChanged.connect(self._sync_navigation)
        output_row.addWidget(self.output_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.clicked.connect(self._browse_output)
        output_row.addWidget(browse)
        form.addRow("Output directory", output_row)
        layout.addLayout(form)
        self.output_note = QLabel()
        self.output_note.setObjectName("Muted")
        self.output_note.setWordWrap(True)
        layout.addWidget(self.output_note)
        self.build_summary = QLabel()
        self.build_summary.setObjectName("Muted")
        self.build_summary.setWordWrap(True)
        layout.addWidget(self.build_summary)
        self.root_check = QCheckBox(
            "I understand this build runs with administrator privileges."
        )
        self.root_check.toggled.connect(self._sync_navigation)
        layout.addWidget(self.root_check)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------- navigation

    def _sync_navigation(self) -> None:
        step = min(self.step, len(STEP_TITLES) - 1)
        running = self._preflight_worker is not None
        self.step_label.setText(
            f"Step {step + 1} of {len(STEP_TITLES)} · {STEP_TITLES[step]}"
        )
        self.back_button.setVisible(step > 0)
        self.next_button.setVisible(step < len(STEP_TITLES) - 1)
        self.start_button.setVisible(step == len(STEP_TITLES) - 1)
        self.back_button.setEnabled(not running)
        self.recheck_button.setEnabled(not running)
        if step < len(STEP_TITLES) - 1:
            self.next_button.setEnabled(
                not running and self._can_continue(step)
            )
        if step == len(STEP_TITLES) - 1:
            self.start_button.setEnabled(self._can_start())

    def _can_continue(self, step: int) -> bool:
        if step == 0:
            return True
        if step == 1:
            if self.blueprint is None:
                return False
            if self.qemu_check.isChecked() and not self._qemu_variant_ready:
                return False
            return True
        if step == 2:
            return self.preflight is not None and not self.preflight.blocking
        return True

    def _can_start(self) -> bool:
        return bool(
            self.output_edit.text().strip() and self.root_check.isChecked()
        )

    def _next(self) -> None:
        if self._preflight_worker is not None:
            return
        if self.step == 1:
            self._resolve_profile()
        if not self._can_continue(self.step):
            return
        if self.step < len(STEP_TITLES) - 1:
            start_preflight = self.step == 1
            self.step += 1
            if self.step == 3:
                self._prepare_build_page()
            self.stack.setCurrentIndex(self.step)
            self._sync_navigation()
            if start_preflight:
                self._run_preflight()

    def _back(self) -> None:
        if self._preflight_worker is not None:
            return
        if self.step > 0:
            self.step -= 1
            self.stack.setCurrentIndex(self.step)
            self._sync_navigation()

    # --------------------------------------------------------------- state

    def _mode_changed(self) -> None:
        self.mode = str(self.mode_combo.currentData() or "qcow2")  # type: ignore[assignment]
        self.qemu_check.setEnabled(self.mode == "qcow2")
        if self.mode != "qcow2" and self.qemu_check.isChecked():
            self.qemu_check.blockSignals(True)
            self.qemu_check.setChecked(False)
            self.qemu_check.blockSignals(False)
            self._resolve_profile()
        self._invalidate_preflight()
        if self.mode == "qcow2":
            self.mode_note.setText(
                "qcow2 is the default lab image. It is managed by vEMU and "
                "can be exported as a template after the build."
            )
        else:
            self.mode_note.setText(
                "Installer ISO is an Advanced bare-metal output. You choose "
                "the output file and write it with your preferred tool."
            )
        self._sync_navigation()

    def _refresh_blueprints(self, select_path: str | None = None) -> None:
        self.blueprints = [
            item for item in self.adapter.blueprints() if item.kind != "iso"
        ]
        self.blueprint_combo.blockSignals(True)
        self.blueprint_combo.clear()
        if not self.blueprints:
            self.blueprint_combo.addItem("No blueprints imported", "")
        else:
            for record in self.blueprints:
                self.blueprint_combo.addItem(
                    f"{record.name} {record.version}".strip(),
                    record.path,
                )
        index = 0
        if select_path:
            for row in range(self.blueprint_combo.count()):
                if self.blueprint_combo.itemData(row) == select_path:
                    index = row
                    break
        self.blueprint_combo.setCurrentIndex(index)
        self.blueprint_combo.blockSignals(False)
        self._blueprint_changed()

    def _blueprint_changed(self) -> None:
        path = str(self.blueprint_combo.currentData() or "")
        self.blueprint = next(
            (item for item in self.blueprints if item.path == path), None
        )
        if self.blueprint is None:
            self.blueprint_name.setText("—")
            self.blueprint_version.setText("—")
            self.blueprint_distro.setText("—")
            self.blueprint_path.setText("—")
            self.blueprint_note.setText(
                "Import a native image-builder TOML/JSON blueprint to "
                "continue."
            )
        else:
            self.blueprint_name.setText(self.blueprint.name)
            self.blueprint_version.setText(self.blueprint.version or "—")
            self.blueprint_distro.setText(self.blueprint.distro)
            self.blueprint_path.setText(self.blueprint.path)
            self.blueprint_note.setText(
                "The blueprint remains the source of truth. Edit it in your "
                "preferred editor; vEMU does not rewrite it."
            )
            if Path(self.blueprint.path).stem.endswith("-qemu"):
                if not self.qemu_check.isChecked():
                    self.qemu_check.blockSignals(True)
                    self.qemu_check.setChecked(True)
                    self.qemu_check.blockSignals(False)
        self._invalidate_preflight()
        if not self._resolving_profile:
            self._resolve_profile()
        self._sync_navigation()

    def _profile_toggled(self, _checked: bool) -> None:
        if self.mode != "qcow2":
            return
        self._resolve_profile()
        self._invalidate_preflight()
        self._sync_navigation()

    def _resolve_profile(self) -> None:
        if self._resolving_profile or self.blueprint is None:
            return
        want = self.qemu_check.isChecked() and self.mode == "qcow2"
        path = Path(self.blueprint.path)
        stem = path.stem
        if want and not stem.endswith("-qemu"):
            target = path.with_name(f"{stem}-qemu{path.suffix}")
        elif not want and stem.endswith("-qemu"):
            target = path.with_name(f"{stem[:-5]}{path.suffix}")
        else:
            self._qemu_variant_ready = True
            self.profile_note.setText(
                "QEMU-optimized variant selected."
                if want and stem.endswith("-qemu")
                else ""
            )
            self._sync_navigation()
            return
        if not target.is_file():
            self._qemu_variant_ready = not want
            if want:
                self.profile_note.setText(
                    "No QEMU-optimized variant was found for this blueprint. "
                    "Import a blueprint-qemu.toml or uncheck the option."
                )
            else:
                self.profile_note.setText("")
            self._sync_navigation()
            return
        self._resolving_profile = True
        try:
            record = self.adapter.import_blueprint(str(target))
            self._qemu_variant_ready = True
            self._refresh_blueprints(select_path=record.path)
            if Path(record.path).stem.endswith("-qemu"):
                self.profile_note.setText(
                    f"Using QEMU-optimized blueprint: {record.name}"
                )
            else:
                self.profile_note.setText("")
        except VemuError as exc:
            self._qemu_variant_ready = False
            self.profile_note.setText(f"Cannot load QEMU variant: {exc}")
        finally:
            self._resolving_profile = False
        self._sync_navigation()

    def _invalidate_preflight(self) -> None:
        self._stop_preflight()
        self.preflight = None
        self._clear_checks()
        self.preflight_summary.setText("Preflight not run yet.")
        self.preflight_note.setText("")
        self._sync_navigation()

    def _stop_preflight(self) -> None:
        worker = self._preflight_worker
        self._preflight_worker = None
        if worker is not None:
            worker.stop()

    def _run_preflight(self) -> None:
        if self.blueprint is None or self._preflight_worker is not None:
            return
        output = self.output_edit.text().strip() or self.adapter.default_output_dir(
            self.mode
        )
        self.preflight = None
        self._clear_checks()
        profile = (
            " · QEMU-optimized"
            if self.qemu_check.isChecked()
            and Path(self.blueprint.path).stem.endswith("-qemu")
            else ""
        )
        self.preflight_summary.setText(
            f"Running preflight · {self.blueprint.name} · "
            f"{self.blueprint.distro} · {self.mode}{profile}"
        )
        self.preflight_note.setText(
            "Checking host tools, packages and repositories. "
            "This can take a minute."
        )
        worker = PreflightWorker(
            self.adapter,
            self.mode,
            self.blueprint,
            output,
            self,
        )
        worker.done.connect(self._preflight_done)
        worker.failed.connect(self._preflight_failed)
        worker.finished.connect(worker.deleteLater)
        self._preflight_worker = worker
        self._sync_navigation()
        worker.start()

    def _preflight_done(self, result: PreflightResult) -> None:
        worker = self._preflight_worker
        if worker is None or self.sender() is not worker:
            return
        self._preflight_worker = None
        self.preflight = result
        self._render_preflight()
        self._sync_navigation()

    def _preflight_failed(self, message: str) -> None:
        worker = self._preflight_worker
        if worker is None or self.sender() is not worker:
            return
        self._preflight_worker = None
        self.preflight = None
        self._clear_checks()
        self.preflight_summary.setText("Preflight failed.")
        self.preflight_note.setText(message)
        self._sync_navigation()

    def done(self, result: int) -> None:
        self._stop_preflight()
        super().done(result)

    def _render_preflight(self) -> None:
        self._clear_checks()
        if self.preflight is None:
            return
        profile = (
            " · QEMU-optimized"
            if self.qemu_check.isChecked()
            and Path(self.blueprint.path).stem.endswith("-qemu")
            else ""
        )
        self.preflight_summary.setText(
            f"{self.blueprint.name} · {self.blueprint.distro} · "
            f"{self.mode}{profile}"
        )
        for check in self.preflight.checks:
            self._add_check(check)
        if self.preflight.blocking:
            self.preflight_note.setText(
                "Fix the blocking checks above before continuing."
            )
        elif self.preflight.warnings:
            self.preflight_note.setText(
                f"Preflight passed with {self.preflight.warnings} warning(s)."
            )
        else:
            self.preflight_note.setText("Preflight passed.")

    def _add_check(self, check: PreflightCheck) -> None:
        row = QHBoxLayout()
        row.setSpacing(8)
        dot = QLabel("●")
        dot.setFixedWidth(12)
        color = {
            "ok": self._tokens.success,
            "warning": self._tokens.warning,
            "error": self._tokens.danger,
        }.get(check.state, self._tokens.muted)
        dot.setStyleSheet(f"color: {color};")
        row.addWidget(dot, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(1)
        label = QLabel(check.label)
        label.setStyleSheet("font-weight: 600;")
        text.addWidget(label)
        if check.detail:
            detail = QLabel(check.detail)
            detail.setObjectName("Muted")
            detail.setWordWrap(True)
            text.addWidget(detail)
        row.addLayout(text, 1)
        self.checks_layout.addLayout(row)

    def _clear_checks(self) -> None:
        while self.checks_layout.count():
            item = self.checks_layout.takeAt(0)
            if item.layout() is not None:
                while item.layout().count():
                    child = item.layout().takeAt(0)
                    widget = child.widget()
                    if widget is not None:
                        widget.deleteLater()
                item.layout().deleteLater()
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _prepare_build_page(self) -> None:
        if not self.output_edit.text().strip():
            self.output_edit.setText(
                self.adapter.default_output_dir(self.mode)
            )
        profile = (
            " · QEMU-optimized"
            if self.qemu_check.isChecked()
            and self.blueprint is not None
            and Path(self.blueprint.path).stem.endswith("-qemu")
            else ""
        )
        self.build_summary.setText(
            f"{self.blueprint.name if self.blueprint else '—'} · "
            f"{self.blueprint.distro if self.blueprint else '—'} · "
            f"{self.mode}{profile}"
        )
        if self.mode == "qcow2":
            self.output_note.setText(
                "Managed by vEMU; the artifact is labeled for libvirt after "
                "the build."
            )
        else:
            self.output_note.setText(
                "User-selected file; vEMU does not register or relabel it. "
                "Write it with your preferred tool."
            )

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Output directory", self.output_edit.text()
        )
        if path:
            self.output_edit.setText(path)

    def _import_blueprint(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import blueprint",
            "",
            "Blueprints (*.toml *.json);;All files (*)",
        )
        if not path:
            return
        try:
            record = self.adapter.import_blueprint(path)
        except VemuError as exc:
            QMessageBox.warning(self, "Import blueprint", str(exc))
            return
        self._refresh_blueprints(select_path=record.path)

    def _start_build(self) -> None:
        if self.blueprint is None or not self._can_start():
            return
        try:
            self.created = self.adapter.start_build(
                self.mode,
                self.blueprint,
                self.output_edit.text().strip(),
                expected_packages=(
                    self.preflight.packages if self.preflight else 0
                ),
            )
        except VemuError as exc:
            QMessageBox.warning(self, "Start build", str(exc))
            return
        self.accept()


__all__ = ["BuildWizardDialog"]
