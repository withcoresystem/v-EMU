"""Seamless-mode dialogs: activation and published-program management."""

from __future__ import annotations

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
)

from ..errors import VemuError
from ..seamless import credentials
from ..seamless.apps import store_aumid
from .dialogs import VemuDialog, dialog_layout

SEAMLESS_VIDEO = {"qxl", "vga"}


class _BackendTask(QThread):
    done = Signal(object, object)

    def __init__(self, operation) -> None:
        super().__init__()
        self._operation = operation

    def run(self) -> None:
        try:
            result = self._operation()
        except Exception as exc:  # noqa: BLE001 - forwarded to the GUI thread
            self.done.emit(None, exc)
        else:
            self.done.emit(result, None)


def _error(parent, title: str, exc: Exception) -> None:
    QMessageBox.critical(parent, title, str(exc))


class SeamlessActivateDialog(VemuDialog):
    """Check the guest on demand, then activate seamless mode."""

    def __init__(self, backend, domain_name: str, parent=None) -> None:
        super().__init__(parent, title="Windows seamless mode")
        self.backend = backend
        self.domain_name = domain_name
        self.changed = False
        self.payload: dict = {}
        self.video = ""
        self.checked = False
        self._precheck_task: _BackendTask | None = None
        self._activate_task: _BackendTask | None = None
        self.setMinimumWidth(680)
        self.resize(720, 520)
        layout = dialog_layout(self)

        title = QLabel(f"Activate Windows seamless mode for {domain_name}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        intro = QLabel(
            "Seamless mode publishes selected Windows programs as Linux "
            "windows over one RAIL connection. Activation first creates a "
            "rollback checkpoint, then the helper applies the guest profile, "
            "disables Edge/Chrome hardware acceleration, installs a pinned "
            "SSH key, enables RDP and records the published program list. "
            "The guest may restart while the checkpoint is created."
        )
        intro.setObjectName("Muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.status = QLabel(
            "Enter the Windows user and password, then click Precheck."
        )
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setFixedHeight(6)
        self.progress.setTextVisible(False)
        self.progress.hide()
        layout.addWidget(self.progress)

        self.details = QLabel()
        self.details.setObjectName("Muted")
        self.details.setWordWrap(True)
        self.details.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self.details, 1)

        form = QHBoxLayout()
        form.addWidget(QLabel("Windows user"))
        self.username = QLineEdit()
        self.username.setPlaceholderText(
            "Windows user (admin or standard), e.g. ProUser"
        )
        form.addWidget(self.username, 1)
        form.addWidget(QLabel("Profile"))
        self.profile = QComboBox()
        self.profile.addItems(["Safe", "Balanced", "3D"])
        form.addWidget(self.profile)
        layout.addLayout(form)

        password_form = QHBoxLayout()
        password_form.addWidget(QLabel("Windows password"))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText(
            "Used for RDP and optionally saved in the keyring"
        )
        password_form.addWidget(self.password, 1)
        layout.addLayout(password_form)

        self.save_password = QCheckBox("Save password in the OS keyring")
        available = credentials.available()
        self.save_password.setChecked(available)
        self.save_password.setEnabled(available)
        if not available:
            self.save_password.setToolTip(
                "Install the 'keyring' extra to avoid typing the password."
            )
        layout.addWidget(self.save_password)

        self.username.textChanged.connect(self._credentials_changed)
        self.password.textChanged.connect(self._credentials_changed)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("SecondaryButton")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.precheck_button = QPushButton("Precheck")
        self.precheck_button.setObjectName("SecondaryButton")
        self.precheck_button.clicked.connect(self._precheck)
        buttons.addWidget(self.precheck_button)
        self.activate_button = QPushButton("Activate")
        self.activate_button.setObjectName("PrimaryButton")
        self.activate_button.clicked.connect(self._activate)
        self.activate_button.setEnabled(False)
        buttons.addWidget(self.activate_button)
        layout.addLayout(buttons)

    def _credentials_changed(self) -> None:
        if self.checked:
            self.checked = False
            self.status.setText(
                "Credentials changed. Run Precheck again."
            )
        self._update_activate()

    def _precheck(self) -> None:
        username = self.username.text().strip()
        password = self.password.text()
        if not username:
            self.status.setText("Enter the Windows user first.")
            return
        if not password:
            self.status.setText(
                "Enter the Windows password first; it is stored for RDP."
            )
            return
        if self._precheck_task is not None and self._precheck_task.isRunning():
            return
        self.checked = False
        self.activate_button.setEnabled(False)
        self.precheck_button.setEnabled(False)
        self.progress.show()
        self.status.setText(
            "Checking Windows edition, QEMU guest agent, RDP and video… "
            "The password is not validated by Precheck."
        )
        task = _BackendTask(self._precheck_worker)
        self._precheck_task = task
        task.done.connect(self._precheck_done)
        task.start()

    def _precheck_worker(self) -> tuple[dict, str]:
        payload = self.backend.vm_seamless_status(self.domain_name)
        video = ""
        try:
            config = self.backend.vm_config_info(self.domain_name)
            video = str((config.get("config") or {}).get("video", ""))
        except (VemuError, AttributeError):
            video = ""
        return payload, video

    def _precheck_done(self, result, error) -> None:
        self.progress.hide()
        self.precheck_button.setEnabled(True)
        if error is not None:
            self.status.setText("Precheck failed.")
            self.details.setText(str(error))
            self.checked = False
            self._update_activate()
            return
        self.payload, self.video = result
        state = self.payload.get("state") or {}
        username = str(state.get("username") or "")
        if username and not self.username.text().strip():
            self.username.setText(username)
        profile = str(state.get("profile") or "Safe")
        index = self.profile.findText(profile)
        if index >= 0:
            self.profile.setCurrentIndex(index)
        self._render()
        preflight = self.payload.get("preflight") or {}
        running = bool(self.payload.get("running"))
        video_ok = self.video in SEAMLESS_VIDEO
        supported = bool(preflight.get("supported", False))
        if not running:
            self.status.setText("Precheck failed: the VM is not running.")
        elif not video_ok:
            self.status.setText(
                "Precheck failed: QXL/VGA video is required. Change the VM "
                "XML to QXL, restart it, then run Precheck again."
            )
        elif not supported:
            reason = self.payload.get("preflight_error") or "guest preflight failed"
            self.status.setText(f"Precheck failed: {reason}")
        else:
            self.checked = True
            self.status.setText(
                "Precheck passed. Review the details and click Activate."
            )
        self._update_activate()

    def _update_activate(self) -> None:
        preflight = self.payload.get("preflight") or {}
        running = bool(self.payload.get("running"))
        video_ok = self.video in SEAMLESS_VIDEO
        supported = bool(preflight.get("supported", False))
        self.activate_button.setEnabled(
            self.checked
            and running
            and video_ok
            and supported
            and bool(self.username.text().strip())
            and bool(self.password.text())
        )

    def _render(self) -> None:
        payload = self.payload
        state = payload.get("state") or {}
        preflight = payload.get("preflight") or {}
        running = bool(payload.get("running"))
        prepared = bool(payload.get("prepared"))
        active = bool(payload.get("active", prepared))
        apps = state.get("apps") or []
        reasons = preflight.get("reasons") or []
        video_ok = self.video in SEAMLESS_VIDEO
        guest = state.get("guest") or {}
        lines = [
            f"<b>Windows:</b> {guest.get('edition_id', 'unknown')} "
            f"build {guest.get('build', '?')}",
            f"<b>Running:</b> {'yes' if running else 'no'} · "
            f"<b>Prepared:</b> {'yes' if prepared else 'no'} · "
            f"<b>Active:</b> {'yes' if active else 'no'}",
            f"<b>Video:</b> {self.video or 'unknown'} "
            + ("(QXL/VGA ✓)" if video_ok else "(requires QXL/VGA)"),
            f"<b>Published programs:</b> {len(apps)}",
        ]
        if preflight:
            lines.append(
                "<b>Guest preflight:</b> "
                + ("supported" if preflight.get("supported") else "blocked")
            )
            for reason in reasons:
                lines.append(f"• {reason}")
        if payload.get("preflight_error"):
            lines.append(f"• {payload['preflight_error']}")
        if not video_ok:
            lines.append(
                "<br><b>QXL/VGA is required.</b> Change the VM XML to QXL or "
                "VGA and restart the guest before activation."
            )
        if prepared:
            lines.append(
                "<br>Activation is already installed; re-running is safe."
                if active
                else "<br>Seamless mode is deactivated. Activate to publish "
                "programs again; the guest setup is kept."
            )
        self.details.setText("<br>".join(lines))

    def _activate(self) -> None:
        if not self.checked:
            return
        username = self.username.text().strip()
        password = self.password.text()
        if not username or not password:
            self.status.setText("Enter the Windows user and password first.")
            return
        self.activate_button.setEnabled(False)
        self.precheck_button.setEnabled(False)
        self.progress.show()
        self.status.setText(
            "Creating the rollback checkpoint and preparing the guest… "
            "This can take a minute while Windows restarts."
        )
        task = _BackendTask(
            lambda: self.backend.vm_seamless_prepare(
                self.domain_name,
                username,
                self.profile.currentText(),
            )
        )
        self._activate_task = task
        task.done.connect(self._activate_done)
        task.start()

    def _activate_done(self, _result, error) -> None:
        self.progress.hide()
        self.precheck_button.setEnabled(True)
        self.activate_button.setEnabled(True)
        if error is not None:
            self.status.setText("Activation failed.")
            _error(self, "Activation failed", error)
            return
        username = self.username.text().strip()
        password = self.password.text()
        if self.save_password.isChecked() and username and password:
            try:
                credentials.save_password(self.domain_name, username, password)
            except VemuError:
                pass
        self.changed = True
        QMessageBox.information(
            self,
            "Seamless Mode",
            "Seamless mode is active. The pre-prepare rollback checkpoint "
            "was created first. Reboot Windows once before first launch so "
            "pending updates and service changes apply. Then use Manage "
            "programs to refresh the guest list, test a launch and publish "
            "shortcuts.",
        )
        self.accept()


class DisableSeamlessDialog(VemuDialog):
    """Choose Pause or Remove when disabling seamless mode."""

    def __init__(
        self,
        domain: str,
        *,
        active: bool,
        checkpoint: bool,
        parent=None,
    ) -> None:
        super().__init__(parent, title="Disable seamless mode")
        self.setMinimumWidth(520)
        self._checkpoint = checkpoint
        layout = dialog_layout(self)
        title = QLabel(f"Disable seamless mode for {domain}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        note = QLabel(
            "Pause is reversible. Remove restores the checkpoint and deletes "
            "host state."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.pause_radio = QRadioButton(
            "Pause — stop publishing, keep Windows setup"
        )
        self.remove_radio = QRadioButton(
            "Remove and restore the pre-seamless checkpoint"
        )
        self.remove_radio.setEnabled(checkpoint)
        if active:
            self.pause_radio.setChecked(True)
        else:
            self.pause_radio.setVisible(False)
            self.remove_radio.setChecked(True)
        if not checkpoint:
            self.remove_radio.setToolTip(
                "No pre-seamless checkpoint is available."
            )
        layout.addWidget(self.pause_radio)
        layout.addWidget(self.remove_radio)

        self.forget_check = QCheckBox("Forget saved Windows password")
        self.forget_check.setChecked(True)
        layout.addWidget(self.forget_check)
        self.pause_radio.toggled.connect(self._sync)
        self.remove_radio.toggled.connect(self._sync)
        self._sync()

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        self.continue_button = QPushButton("Continue")
        self.continue_button.setObjectName("PrimaryButton")
        self.continue_button.clicked.connect(self.accept)
        buttons.addWidget(self.continue_button)
        layout.addLayout(buttons)

    def _sync(self) -> None:
        remove = self.remove_radio.isChecked()
        self.forget_check.setVisible(remove)
        self.forget_check.setEnabled(remove)

    def selection(self) -> tuple[str, bool]:
        mode = "remove" if self.remove_radio.isChecked() else "pause"
        return mode, bool(self.forget_check.isChecked())


class PasswordDialog(VemuDialog):
    """Ask for the Windows password and optionally save it in the keyring."""

    def __init__(self, username: str, parent=None) -> None:
        super().__init__(parent, title="Windows password")
        self.setMinimumWidth(480)
        layout = dialog_layout(self)
        title = QLabel(f"RDP password for {username or 'the guest'}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("Windows account password")
        layout.addWidget(self.password)
        self.save = QCheckBox("Save in the OS keyring")
        available = credentials.available()
        self.save.setChecked(available)
        self.save.setEnabled(available)
        if not available:
            self.save.setToolTip(
                "Install the 'keyring' extra to avoid typing the password."
            )
        layout.addWidget(self.save)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        ok = QPushButton("Continue")
        ok.setObjectName("PrimaryButton")
        ok.clicked.connect(self._accept)
        buttons.addWidget(ok)
        layout.addLayout(buttons)

    def _accept(self) -> None:
        if not self.password.text():
            QMessageBox.warning(
                self, "Windows password", "Enter the Windows password."
            )
            return
        self.accept()


class AddProgramDialog(VemuDialog):
    """Pick an installed program or enter one manually."""

    def __init__(self, available: list[dict], parent=None) -> None:
        super().__init__(parent, title="Add Windows program")
        self.available = available
        self.selection: dict | None = None
        self.setMinimumWidth(620)
        layout = dialog_layout(self)
        title = QLabel("Add a Windows program")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)
        layout.addWidget(
            QLabel(
                "Refresh first to list Start-menu and optional Store/MSIX "
                "apps, or enter a path manually. Chromium/Electron programs "
                "receive software-GPU flags automatically."
            )
        )
        self.picker = QComboBox()
        self.picker.setEditable(False)
        self.picker.addItem("Manual path…", None)
        for item in available:
            detail = (
                "Store app"
                if item.get("source") == "store"
                else item.get("path")
            )
            self.picker.addItem(f"{item.get('name')}  —  {detail}", item)
        self.picker.currentIndexChanged.connect(self._pick)
        layout.addWidget(self.picker)
        form = QHBoxLayout()
        form.addWidget(QLabel("Display name"))
        self.name = QLineEdit()
        form.addWidget(self.name, 1)
        layout.addLayout(form)
        form = QHBoxLayout()
        form.addWidget(QLabel("Executable path"))
        self.path = QLineEdit()
        form.addWidget(self.path, 1)
        layout.addLayout(form)
        form = QHBoxLayout()
        form.addWidget(QLabel("Launch arguments"))
        self.arguments = QLineEdit()
        form.addWidget(self.arguments, 1)
        layout.addLayout(form)
        self.compat = QCheckBox(
            "Add --disable-gpu --disable-gpu-compositing for Chromium/Electron"
        )
        self.compat.setChecked(True)
        layout.addWidget(self.compat)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setObjectName("SecondaryButton")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        add = QPushButton("Add")
        add.setObjectName("PrimaryButton")
        add.clicked.connect(self._add)
        buttons.addWidget(add)
        layout.addLayout(buttons)

    def _pick(self, _index: int) -> None:
        item = self.picker.currentData()
        if not isinstance(item, dict):
            return
        self.name.setText(str(item.get("name", "")))
        self.path.setText(str(item.get("path", "")))
        self.arguments.setText(str(item.get("arguments", "")))

    def _add(self) -> None:
        name = self.name.text().strip()
        path = self.path.text().strip()
        if not name or not path:
            QMessageBox.warning(
                self, "Add program", "Name and path are required."
            )
            return
        self.selection = {
            "name": name,
            "path": path,
            "arguments": self.arguments.text().strip(),
            "compat": self.compat.isChecked(),
        }
        self.accept()


class SeamlessProgramsDialog(VemuDialog):
    """Refresh, test, add and remove published programs/shortcuts."""

    def __init__(self, backend, domain_name: str, parent=None) -> None:
        super().__init__(parent, title="Manage Windows programs")
        self.backend = backend
        self.domain_name = domain_name
        self.changed = False
        self.available: list[dict] = []
        self.state: dict = {}
        self.active = False
        self._task: _BackendTask | None = None
        self.setMinimumWidth(820)
        self.resize(900, 560)
        layout = dialog_layout(self)

        title = QLabel(f"Windows programs published from {domain_name}")
        title.setObjectName("DialogTitle")
        layout.addWidget(title)

        self.status = QLabel()
        self.status.setObjectName("Muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        options = QHBoxLayout()
        self.show_system = QCheckBox(
            "Show built-in Windows tools (C:\\Windows)"
        )
        options.addWidget(self.show_system)
        self.show_store = QCheckBox(
            "Show Store/MSIX apps (Calculator, Paint, …)"
        )
        self.show_store.setToolTip(
            "Packaged apps are launched through explorer.exe "
            "shell:AppsFolder with their friendly name."
        )
        options.addWidget(self.show_store)
        options.addStretch(1)
        layout.addLayout(options)

        self.show_system.toggled.connect(self._options_changed)
        self.show_store.toggled.connect(self._options_changed)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Program", "Location", "Status", "Launch arguments"]
        )
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)

        buttons = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("SecondaryButton")
        self.refresh_button.clicked.connect(self._refresh)
        buttons.addWidget(self.refresh_button)
        self.add_button = QPushButton("Add…")
        self.add_button.setObjectName("SecondaryButton")
        self.add_button.clicked.connect(self._add)
        buttons.addWidget(self.add_button)
        self.test_button = QPushButton("Test launch…")
        self.test_button.setObjectName("SecondaryButton")
        self.test_button.clicked.connect(self._test)
        buttons.addWidget(self.test_button)
        self.remove_button = QPushButton("Remove")
        self.remove_button.setObjectName("SecondaryButton")
        self.remove_button.clicked.connect(self._remove)
        buttons.addWidget(self.remove_button)
        self.forget_button = QPushButton("Forget password")
        self.forget_button.setObjectName("SecondaryButton")
        self.forget_button.clicked.connect(self._forget_password)
        buttons.addWidget(self.forget_button)
        buttons.addStretch(1)
        close = QPushButton("Close")
        close.setObjectName("PrimaryButton")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        layout.addLayout(buttons)

        self._load()

    def _load(self) -> None:
        try:
            if hasattr(self.backend, "vm_seamless_state"):
                payload = self.backend.vm_seamless_state(self.domain_name)
            else:
                payload = self.backend.vm_seamless_status(self.domain_name)
        except (VemuError, AttributeError) as exc:
            self.status.setText(str(exc))
            self._enable(False)
            return
        self.state = payload.get("state") or {}
        self.active = bool(payload.get("active", bool(self.state)))
        available = self.state.get("available_apps") or []
        if not self.show_store.isChecked():
            available = [
                item
                for item in available
                if isinstance(item, dict) and item.get("source") != "store"
            ]
        self.available = list(available)
        self._enable(self.active)
        self._render()
        self._update_forget()
        if not self.state:
            self.status.setText(
                "Activate Seamless mode first, then refresh and publish "
                "programs here."
            )
        elif not self.active:
            self.status.setText(
                "Seamless mode is deactivated. Use Activate Seamless mode "
                "to publish programs again."
            )

    def _enable(self, enabled: bool) -> None:
        for button in (
            self.refresh_button,
            self.add_button,
            self.test_button,
            self.remove_button,
        ):
            button.setEnabled(enabled)

    def _update_forget(self) -> None:
        username = str(self.state.get("username") or "")
        self.forget_button.setEnabled(
            bool(username) and credentials.available()
        )

    def _set_busy(self, busy: bool, message: str = "") -> None:
        if busy:
            for button in (
                self.refresh_button,
                self.add_button,
                self.test_button,
                self.remove_button,
                self.forget_button,
            ):
                button.setEnabled(False)
            self.status.setText(message or "Working…")
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            QApplication.processEvents()
        else:
            if QApplication.overrideCursor() is not None:
                QApplication.restoreOverrideCursor()
            self._enable(self.active)
            self._update_forget()

    def _render(self) -> None:
        apps = [item for item in self.state.get("apps") or [] if isinstance(item, dict)]
        self.table.setRowCount(len(apps))
        for row, app in enumerate(apps):
            name = str(app.get("name", ""))
            path = str(app.get("path", ""))
            if store_aumid(str(app.get("arguments") or "")):
                path = "Store app"
            verified = bool(app.get("verified"))
            self.table.setItem(row, 0, QTableWidgetItem(name))
            self.table.setItem(row, 1, QTableWidgetItem(path))
            self.table.setItem(
                row,
                2,
                QTableWidgetItem("Verified" if verified else "Unverified"),
            )
            arguments = str(app.get("arguments", ""))
            if store_aumid(arguments):
                arguments = "Windows package"
            self.table.setItem(row, 3, QTableWidgetItem(arguments))
            self.table.item(row, 0).setData(Qt.ItemDataRole.UserRole, app)
        if self.state:
            store = (
                "Store apps shown"
                if self.show_store.isChecked()
                else "Store apps hidden"
            )
            self.status.setText(
                f"{len(apps)} published program(s) · "
                f"{len(self.available)} discovered · {store} · "
                "shortcuts share one RAIL connection when the supervisor runs."
            )

    def _selected(self) -> dict | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _options_changed(self) -> None:
        if self.active and (
            self._task is None or not self._task.isRunning()
        ):
            self._refresh()

    def _refresh(self) -> None:
        if self._task is not None and self._task.isRunning():
            return
        self._set_busy(
            True, "Refreshing Windows programs…"
        )
        include_system = self.show_system.isChecked()
        include_store = self.show_store.isChecked()
        task = _BackendTask(
            lambda: self.backend.vm_seamless_refresh_apps(
                self.domain_name,
                include_system=include_system,
                include_store=include_store,
            )
        )
        task.done.connect(self._refresh_done)
        self._task = task
        task.start()

    def _refresh_done(self, result, error) -> None:
        self._set_busy(False)
        if error is not None:
            _error(self, "Refresh failed", error)
            return
        self.available = list(result.get("available") or [])
        self.changed = True
        self._load()
        missing = result.get("missing") or []
        new = result.get("new") or []
        notes = []
        if new:
            notes.append(f"{len(new)} new program(s)")
        if missing:
            notes.append(
                f"{len(missing)} published program(s) no longer found"
            )
        if notes:
            QMessageBox.information(
                self, "Refresh", "; ".join(notes) + "."
            )

    def _add(self) -> None:
        dialog = AddProgramDialog(self.available, self)
        if (
            dialog.exec() != QDialog.DialogCode.Accepted
            or not dialog.selection
        ):
            return
        entry = dialog.selection
        self._add_entry = entry
        self._set_busy(True, f"Adding {entry['name']}…")
        task = _BackendTask(
            lambda: self.backend.vm_seamless_add_app(
                self.domain_name,
                entry["name"],
                entry["path"],
                entry.get("arguments", ""),
                entry.get("compat", True),
                False,
            )
        )
        task.done.connect(self._add_done)
        self._task = task
        task.start()

    def _add_done(self, result, error) -> None:
        self._set_busy(False)
        entry = getattr(self, "_add_entry", {"name": "program"})
        if error is not None:
            _error(self, "Add program failed", error)
            return
        self.changed = True
        self._load()
        shortcut = (result.get("entry") or {}).get("shortcut")
        message = (
            f"Added {entry['name']} · "
            f"{len(self.state.get('apps') or [])} published program(s)."
        )
        if shortcut:
            message += f" Shortcut: {shortcut}"
        self.status.setText(message)

    def _test(self) -> None:
        app = self._selected()
        if not app:
            QMessageBox.information(
                self, "Test launch", "Select a program first."
            )
            return
        username = str(self.state.get("username") or "")
        password = ""
        if username:
            try:
                password = (
                    credentials.get_password(self.domain_name, username) or ""
                )
            except VemuError:
                password = ""
        save = False
        if not password:
            dialog = PasswordDialog(username, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            password = dialog.password.text()
            save = dialog.save.isChecked()
        from .. import service

        name = str(app.get("name", ""))
        self._test_app_name = name
        self._set_busy(
            True,
            f"Testing {name}… the window stays responsive; "
            "this can take up to 30 seconds.",
        )

        def operation():
            result = service.vm_seamless_test_app(
                self.domain_name, name, password
            )
            if result.get("ok"):
                if save and username:
                    try:
                        credentials.save_password(
                            self.domain_name, username, password
                        )
                    except VemuError:
                        pass
                try:
                    self.backend.vm_seamless_add_app(
                        self.domain_name,
                        name,
                        str(app.get("path", "")),
                        str(app.get("arguments", "")),
                        False,
                        True,
                    )
                except (VemuError, AttributeError):
                    pass
            return result

        task = _BackendTask(operation)
        task.done.connect(self._test_done)
        self._task = task
        task.start()

    def _test_done(self, result, error) -> None:
        self._set_busy(False)
        name = getattr(self, "_test_app_name", "program")
        if error is not None:
            _error(self, "Test launch failed", error)
            return
        paused = result.get("stopped_other_supervisors") or []
        paused_note = (
            "\n\nPaused the RAIL session for: " + ", ".join(paused) + "."
            if paused
            else ""
        )
        if result.get("ok"):
            QMessageBox.information(
                self,
                "Test launch",
                f"{name} connected and stayed open. It is marked Verified. "
                "This checks the RAIL connection; verify the app window and "
                "its dialogs manually." + paused_note,
            )
            self.changed = True
            self._load()
        else:
            QMessageBox.warning(
                self,
                "Test launch",
                f"{name} failed: {result.get('reason')}.\n\n"
                "If the saved password is wrong, use Forget password and "
                "try again." + paused_note,
            )

    def _forget_password(self) -> None:
        username = str(self.state.get("username") or "")
        if not username:
            return
        try:
            removed = credentials.delete_password(
                self.domain_name, username
            )
        except VemuError as exc:
            _error(self, "Forget password", exc)
            return
        QMessageBox.information(
            self,
            "Forget password",
            f"Saved password for {username} "
            + ("removed." if removed else "was not present."),
        )
        self._update_forget()

    def _remove(self) -> None:
        app = self._selected()
        if not app:
            QMessageBox.information(
                self, "Remove program", "Select a program first."
            )
            return
        name = str(app.get("name", ""))
        answer = QMessageBox.question(
            self,
            "Remove Windows program",
            f"Remove the published shortcut and RemoteApp allowlist entry "
            f"for '{name}'?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = self.backend.vm_seamless_remove_app(
                self.domain_name, name
            )
        except Exception as exc:  # noqa: BLE001 - show the real UI failure
            _error(self, "Remove failed", exc)
            return
        self.changed = True
        self._load()
        if result.get("warning"):
            QMessageBox.warning(self, "Remove program", result["warning"])

    def closeEvent(self, event) -> None:
        if self._task is not None and self._task.isRunning():
            QMessageBox.information(
                self,
                "Manage Windows programs",
                "A test launch or refresh is still running. Wait for it to "
                "finish before closing this window.",
            )
            event.ignore()
            return
        super().closeEvent(event)
