"""Five-step Image Builder wizard for installer ISOs.

Steps: Blueprint -> Branding -> Firstboot -> Preflight -> Build.
The qcow2 path keeps its original four-step wizard.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ..errors import VemuError
from ..imagebuilder import BlueprintRecord, BuildJob, PreflightCheck, PreflightResult
from ..iso_builder import IsoBranding, IsoFirstboot, IsoOptions, validate_options
from .components import form_layout
from .dialogs import VemuDialog, dialog_layout
from .image_builder_wizard import PreflightWorker
from .theme import DARK, LIGHT
from .widgets import ComboBox

STEP_TITLES = ("Blueprint", "Branding", "Firstboot", "Preflight", "Build")


class IsoBuildWizardDialog(VemuDialog):
    def __init__(self, adapter, *, dark: bool = True, parent=None) -> None:
        super().__init__(parent, title="New installer ISO")
        self.adapter = adapter
        self.dark = dark
        self._tokens = DARK if dark else LIGHT
        self.mode = "iso"
        self.blueprints: list[BlueprintRecord] = []
        self.blueprint: BlueprintRecord | None = None
        self.preflight: PreflightResult | None = None
        self.created: BuildJob | None = None
        self._preflight_worker: PreflightWorker | None = None
        self.step = 0
        self.resize(780, 640)

        layout = dialog_layout(self)
        title = QLabel("New installer ISO")
        title.setObjectName("HeroTitle")
        layout.addWidget(title)
        subtitle = QLabel(
            "The blueprint provides the OS payload. Branding and the "
            "first-boot TUI are optional vEMU layers."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        layout.addWidget(subtitle)
        self.step_label = QLabel()
        self.step_label.setObjectName("Muted")
        layout.addWidget(self.step_label)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_blueprint_page())
        self.stack.addWidget(self._build_branding_page())
        self.stack.addWidget(self._build_firstboot_page())
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
        self.start_button = QPushButton("Start ISO build")
        self.start_button.setObjectName("PrimaryButton")
        self.start_button.clicked.connect(self._start_build)
        buttons.addWidget(self.start_button)
        layout.addLayout(buttons)

        self._refresh_blueprints()
        self._sync_navigation()

    # ------------------------------------------------------------------ pages

    def _build_blueprint_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        row = QHBoxLayout()
        self.blueprint_combo = ComboBox()
        self.blueprint_combo.currentIndexChanged.connect(self._blueprint_changed)
        row.addWidget(self.blueprint_combo, 1)
        self.import_button = QPushButton("Import ISO blueprint…")
        self.import_button.setObjectName("SecondaryButton")
        self.import_button.clicked.connect(self._import_blueprint)
        row.addWidget(self.import_button)
        layout.addLayout(row)

        form = form_layout()
        self.blueprint_name = QLabel("—")
        self.blueprint_distro = QLabel("—")
        self.blueprint_path = QLabel("—")
        self.blueprint_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.blueprint_path.setWordWrap(True)
        form.addRow("Name", self.blueprint_name)
        form.addRow("Distro", self.blueprint_distro)
        form.addRow("Path", self.blueprint_path)
        layout.addLayout(form)

        self.blueprint_note = QLabel()
        self.blueprint_note.setObjectName("Muted")
        self.blueprint_note.setWordWrap(True)
        layout.addWidget(self.blueprint_note)
        layout.addStretch(1)
        return page

    def _build_branding_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.brand_check = QCheckBox(
            "Brand Anaconda, GRUB and the Plymouth boot screen"
        )
        self.brand_check.toggled.connect(self._sync_navigation)
        layout.addWidget(self.brand_check)

        form = form_layout()
        self.product = QLineEdit("vEMU Lab")
        self.version = QLineEdit("0.2")
        self.sidebar = QLineEdit("#111827")
        self.accent = QLineEdit("#0f766e")
        self.grub_title = QLineEdit("Install vEMU Lab")
        self.logo = QPlainTextEdit()
        self.logo.setFixedHeight(118)
        self.logo.setPlaceholderText(
            "Paste SVG code. vEMU fits it to 150x69 (installer) and 280x240 "
            "(boot screen) without recolouring."
        )
        try:
            self.logo.setPlainText(
                (Path(__file__).with_name("assets") / "org.vemu.VEMU.svg").read_text(
                    encoding="utf-8"
                )
            )
        except OSError:
            pass
        form.addRow("Product name", self.product)
        form.addRow("Product version", self.version)
        form.addRow("Sidebar HEX", self.sidebar)
        form.addRow("Accent HEX", self.accent)
        form.addRow("GRUB title", self.grub_title)
        form.addRow("Logo SVG", self.logo)
        layout.addLayout(form)

        self.brand_note = QLabel()
        self.brand_note.setObjectName("Muted")
        self.brand_note.setWordWrap(True)
        layout.addWidget(self.brand_note)
        layout.addStretch(1)
        return page

    def _build_firstboot_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.firstboot_check = QCheckBox(
            "Enable the fullscreen first-boot TUI"
        )
        self.firstboot_check.toggled.connect(self._sync_navigation)
        layout.addWidget(self.firstboot_check)

        form = form_layout()
        self.company = QLineEdit("CORESYSTEM")
        self.retries = QSpinBox()
        self.retries.setRange(0, 20)
        self.retries.setValue(3)
        self.usb = QCheckBox("USB stick (VEMU-FBOOT)")
        self.usb.setChecked(True)
        self.url = QCheckBox("HTTP(S) URL")
        self.url.setChecked(True)
        sources = QHBoxLayout()
        sources.addWidget(self.usb)
        sources.addWidget(self.url)
        sources.addStretch(1)
        form.addRow("Company name", self.company)
        form.addRow("Sources", sources)
        form.addRow("Retries", self.retries)

        automatic = QLabel("Automatic payload (optional)")
        automatic.setObjectName("SectionLabel")
        form.addRow("", automatic)
        self.script_name = QLineEdit()
        self.script_name.setPlaceholderText("site-setup.sh")
        self.script_name.setClearButtonEnabled(True)
        form.addRow("Script name", self.script_name)
        self.script_url = QLineEdit()
        self.script_url.setPlaceholderText(
            "https://deploy.example/firstboot/"
        )
        self.script_url.setClearButtonEnabled(True)
        form.addRow("Script URL", self.script_url)
        self.firstboot_check.toggled.connect(self._firstboot_toggled)
        self._firstboot_toggled(self.firstboot_check.isChecked())
        layout.addLayout(form)

        self.firstboot_note = QLabel(
            "The TUI runs once on tty2 before the display manager. Leave both "
            "script fields empty for the manual menu. When a script is set, "
            "the TUI tries the URL (and USB fallback) automatically, then "
            "restarts after success; failures return to the menu. Keep "
            "python3 in the ISO blueprint."
        )
        self.firstboot_note.setObjectName("Muted")
        self.firstboot_note.setWordWrap(True)
        layout.addWidget(self.firstboot_note)
        layout.addStretch(1)
        return page

    def _firstboot_toggled(self, checked: bool) -> None:
        for widget in (
            self.company,
            self.retries,
            self.usb,
            self.url,
            self.script_name,
            self.script_url,
        ):
            widget.setEnabled(checked)

    def _build_preflight_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
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
        row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.textChanged.connect(self._sync_navigation)
        row.addWidget(self.output_edit, 1)
        browse = QPushButton("Browse…")
        browse.setObjectName("SecondaryButton")
        browse.clicked.connect(self._browse_output)
        row.addWidget(browse)
        form.addRow("Output directory", row)
        layout.addLayout(form)
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

    # --------------------------------------------------------------- state

    def _refresh_blueprints(self, select_path: str | None = None) -> None:
        self.blueprints = [
            item for item in self.adapter.blueprints() if item.kind == "iso"
        ]
        self.blueprint_combo.blockSignals(True)
        self.blueprint_combo.clear()
        if not self.blueprints:
            self.blueprint_combo.addItem("No ISO blueprints imported", "")
        else:
            for record in self.blueprints:
                self.blueprint_combo.addItem(
                    f"{record.name} {record.version}".strip(), record.path
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
            self.blueprint_distro.setText("—")
            self.blueprint_path.setText("—")
            self.blueprint_note.setText(
                "Import an ISO blueprint such as blueprint-iso.toml. "
                "The qcow2 template is intentionally hidden here."
            )
        else:
            self.blueprint_name.setText(self.blueprint.name)
            self.blueprint_distro.setText(self.blueprint.distro)
            self.blueprint_path.setText(self.blueprint.path)
            self.blueprint_note.setText(
                "Blueprint provides the liveimg payload. ISO-specific "
                "cleanup and optional firstboot are generated by vEMU."
            )
        self._invalidate_preflight()
        self._sync_navigation()

    def _import_blueprint(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Import ISO blueprint",
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
        if record.kind != "iso":
            QMessageBox.information(
                self,
                "Import ISO blueprint",
                "This is a qcow2 blueprint. Use a blueprint-iso.toml file "
                "for the installer ISO workflow.",
            )
        self._refresh_blueprints(select_path=record.path if record.kind == "iso" else None)

    # ------------------------------------------------------------- navigation

    def _sync_navigation(self) -> None:
        running = self._preflight_worker is not None
        self.step_label.setText(
            f"Step {self.step + 1} of {len(STEP_TITLES)} · {STEP_TITLES[self.step]}"
        )
        self.back_button.setVisible(self.step > 0)
        self.next_button.setVisible(self.step < len(STEP_TITLES) - 1)
        self.start_button.setVisible(self.step == len(STEP_TITLES) - 1)
        self.back_button.setEnabled(not running)
        self.recheck_button.setEnabled(not running)
        if self.step < len(STEP_TITLES) - 1:
            self.next_button.setEnabled(not running and self._can_continue(self.step))
        if self.step == len(STEP_TITLES) - 1:
            self.start_button.setEnabled(self._can_start())

    def _can_continue(self, step: int) -> bool:
        if step == 0:
            return self.blueprint is not None
        if step == 1:
            if not self.brand_check.isChecked():
                self.brand_note.setText("Branding disabled; ISO uses the distro branding.")
                return True
            try:
                validate_options(IsoOptions(branding=self._branding()))
            except ValueError as exc:
                self.brand_note.setText(str(exc))
                return False
            self.brand_note.setText(
                "Branding will be written to the installer, GRUB and the "
                "installed boot screen."
            )
            return True
        if step == 2:
            try:
                validate_options(IsoOptions(firstboot=self._firstboot()))
            except ValueError as exc:
                if hasattr(self, "firstboot_note"):
                    self.firstboot_note.setText(str(exc))
                return False
            if hasattr(self, "firstboot_note"):
                self.firstboot_note.setText(
                    "The TUI runs once on tty2; the payload is removed after "
                    "the setup or a confirmed quit."
                    if self.firstboot_check.isChecked()
                    else "Firstboot TUI disabled; only the cleanup kickstart runs."
                )
            return True
        if step == 3:
            return self.preflight is not None and not self.preflight.blocking
        return True

    def _can_start(self) -> bool:
        return bool(self.output_edit.text().strip() and self.root_check.isChecked())

    def _next(self) -> None:
        if self._preflight_worker is not None:
            return
        if not self._can_continue(self.step):
            return
        if self.step < len(STEP_TITLES) - 1:
            run_preflight = self.step == 2
            self.step += 1
            if self.step == 4:
                self._prepare_build_page()
            self.stack.setCurrentIndex(self.step)
            self._sync_navigation()
            if run_preflight:
                self._run_preflight()

    def _back(self) -> None:
        if self._preflight_worker is not None:
            return
        if self.step > 0:
            self.step -= 1
            self.stack.setCurrentIndex(self.step)
            self._sync_navigation()

    # --------------------------------------------------------------- options

    def _branding(self) -> IsoBranding | None:
        if not self.brand_check.isChecked():
            return None
        return IsoBranding(
            product_name=self.product.text().strip(),
            product_version=self.version.text().strip(),
            sidebar_bg=self.sidebar.text().strip(),
            accent=self.accent.text().strip(),
            logo_svg=self.logo.toPlainText(),
            grub_title=self.grub_title.text().strip(),
        )

    def _firstboot(self) -> IsoFirstboot | None:
        if not self.firstboot_check.isChecked():
            return None
        sources = []
        if self.usb.isChecked():
            sources.append("usb")
        if self.url.isChecked():
            sources.append("url")
        return IsoFirstboot(
            company_name=self.company.text().strip(),
            sources=tuple(sources),
            retries=int(self.retries.value()),
            script_name=self.script_name.text().strip(),
            script_url=self.script_url.text().strip(),
        )

    def iso_options(self) -> IsoOptions:
        return IsoOptions(branding=self._branding(), firstboot=self._firstboot())

    # ------------------------------------------------------------- preflight

    def _invalidate_preflight(self) -> None:
        self._stop_preflight()
        self.preflight = None
        self._clear_checks()
        if hasattr(self, "preflight_summary"):
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
        output = self.output_edit.text().strip() or self.adapter.default_output_dir("iso")
        self.preflight = None
        self._clear_checks()
        self.preflight_summary.setText(
            f"Running preflight · {self.blueprint.name} · {self.blueprint.distro} · iso"
        )
        self.preflight_note.setText("Checking ISO tools, packages and customization.")
        worker = PreflightWorker(
            self.adapter,
            "iso",
            self.blueprint,
            output,
            self,
            iso_options=self.iso_options(),
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

    def _render_preflight(self) -> None:
        self._clear_checks()
        if self.preflight is None:
            return
        self.preflight_summary.setText(
            f"{self.blueprint.name if self.blueprint else '—'} · iso"
        )
        for check in self.preflight.checks:
            self._add_check(check)
        if self.preflight.blocking:
            self.preflight_note.setText("Fix the blocking checks above.")
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
        if not hasattr(self, "checks_layout"):
            return
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
            self.output_edit.setText(self.adapter.default_output_dir("iso"))
        options = self.iso_options()
        extras = []
        if options.branding:
            extras.append("branded")
        if options.firstboot:
            extras.append("firstboot TUI")
        self.build_summary.setText(
            f"{self.blueprint.name if self.blueprint else '—'} · "
            f"{self.blueprint.distro if self.blueprint else '—'} · iso"
            + (" · " + " + ".join(extras) if extras else "")
        )

    def _browse_output(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Output directory", self.output_edit.text()
        )
        if path:
            self.output_edit.setText(path)

    def _start_build(self) -> None:
        if self.blueprint is None or not self._can_start():
            return
        try:
            self.created = self.adapter.start_build(
                "iso",
                self.blueprint,
                self.output_edit.text().strip(),
                expected_packages=self.preflight.packages if self.preflight else 0,
                iso_options=self.iso_options(),
            )
        except VemuError as exc:
            QMessageBox.warning(self, "Start ISO build", str(exc))
            return
        self.accept()

    def done(self, result: int) -> None:
        self._stop_preflight()
        super().done(result)


__all__ = ["IsoBuildWizardDialog"]
