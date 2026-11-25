"""Image Builder page shell (Images and Blueprints tabs)."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..errors import VemuError
from ..imagebuilder import (
    BlueprintRecord,
    BuildJob,
    ImageRecord,
    RealImageBuilderAdapter,
    default_adapter,
)
from .build_window import BuildWindow
from .components import PageHeader
from .delegate import NoFocusDelegate
from .image_builder_iso_wizard import IsoBuildWizardDialog
from .image_builder_wizard import BuildWizardDialog
from .image_packages import ImagePackageLookupDialog
from .osbuild_catalog import OSBuildCatalogDialog
from .page_tabs import PageTabs
from .theme import DARK, LIGHT
from .tokens import COMPONENT, SPACE
from .tooltips import set_tooltip
from .widgets import EmptyStateTable


class ImageBuilderPage(QWidget):
    status = Signal(str)
    template_imported = Signal(str)

    TERMINAL_STATES = {"Succeeded", "Failed", "Cancelled"}
    BLUEPRINT_KEY = "imagebuilder/blueprints"
    SELECTED_BLUEPRINT_KEY = "imagebuilder/selected_blueprint"

    def __init__(
        self,
        backend=None,
        *,
        dark: bool = True,
        parent=None,
        adapter=None,
        templates_page=None,
    ) -> None:
        super().__init__(parent)
        self.backend = backend
        self.dark = dark
        self.adapter = adapter or default_adapter()
        self.templates_page = templates_page
        self._settings = QSettings("v-EMU", "v-EMU")
        self._tokens = DARK if dark else LIGHT
        self._storage_checked = False
        self.blueprints: list[BlueprintRecord] = []
        self.images = []
        self.build_jobs: list[BuildJob] = []
        self.build_windows: dict[int, BuildWindow] = {}
        self._storage_timer = QTimer(self)
        self._storage_timer.setSingleShot(True)
        self._storage_timer.timeout.connect(self._ensure_images_storage)
        self._restore_blueprints()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            COMPONENT.page_margin,
            COMPONENT.page_top,
            COMPONENT.page_margin,
            COMPONENT.page_bottom,
        )
        layout.setSpacing(SPACE.lg)

        header = QHBoxLayout()
        header.addWidget(
            PageHeader(
                "Image Builder",
                "Build and manage qcow2 images and installer ISOs.",
            )
        )
        header.addStretch(1)
        self.import_button = QPushButton("Import blueprint")
        self.import_button.setObjectName("SecondaryButton")
        self.import_button.clicked.connect(self._import_blueprint)
        header.addWidget(self.import_button)
        self.new_button = QPushButton("New qcow2 build")
        self.new_button.setObjectName("PrimaryButton")
        self.new_button.setToolTip("Start a new qcow2 image build (Ctrl+N)")
        self.new_button.clicked.connect(self.new_build)
        header.addWidget(self.new_button)
        self.new_iso_button = QPushButton("New ISO build")
        self.new_iso_button.setObjectName("SecondaryButton")
        self.new_iso_button.setToolTip(
            "Start an installer ISO build with optional branding/firstboot"
        )
        self.new_iso_button.clicked.connect(self.new_iso_build)
        header.addWidget(self.new_iso_button)
        layout.addLayout(header)

        host_row = QHBoxLayout()
        host_row.setSpacing(SPACE.sm)
        self.host_dot = QLabel("●")
        self.host_dot.setFixedWidth(12)
        host_row.addWidget(self.host_dot)
        self.host_text = QLabel()
        self.host_text.setObjectName("Muted")
        host_row.addWidget(self.host_text)
        host_row.addStretch(1)
        self.catalog_button = QPushButton("OSBuild Catalog")
        self.catalog_button.setObjectName("SecondaryButton")
        set_tooltip(
            self.catalog_button,
            "Browse the distro, architecture and image-type combinations "
            "that image-builder can build on this host.",
        )
        self.catalog_button.clicked.connect(self._open_catalog)
        host_row.addWidget(self.catalog_button)
        self.packages_button = QPushButton("Package lookup")
        self.packages_button.setObjectName("SecondaryButton")
        set_tooltip(
            self.packages_button,
            "Search packages and package groups in the target distro "
            "repositories before adding them to a blueprint.",
        )
        self.packages_button.clicked.connect(self._open_packages)
        host_row.addWidget(self.packages_button)
        self.recheck_button = QPushButton("Recheck")
        self.recheck_button.setObjectName("SecondaryButton")
        set_tooltip(
            self.recheck_button,
            "Re-run host checks: OS family, image-builder, osbuild, "
            "osbuild-depsolve-dnf, qemu-img, rpm, dnf and the privilege path.",
        )
        self.recheck_button.clicked.connect(self.refresh)
        host_row.addWidget(self.recheck_button)
        layout.addLayout(host_row)

        self.host_notice = QLabel()
        self.host_notice.setObjectName("Warning")
        self.host_notice.setWordWrap(True)
        self.host_notice.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.host_notice.hide()
        layout.addWidget(self.host_notice)

        self.tabs = PageTabs(["Images", "Blueprints"])
        self.stack = QStackedWidget()
        self.tabs.currentChanged.connect(self._tab_changed)
        layout.addWidget(self.tabs, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.stack, 1)

        self.images_tab = self._build_images_tab()
        self.blueprints_tab = self._build_blueprints_tab()
        self.stack.addWidget(self.images_tab)
        self.stack.addWidget(self.blueprints_tab)

        # Always open on Images. A saved tab from an earlier session used to
        # reopen the page on Blueprints after an import, which is confusing
        # until the empty Images table has been seen at least once.
        self.tabs.setCurrentIndex(0)
        self.stack.setCurrentIndex(0)

        self.refresh()

    # ------------------------------------------------------------------ tabs

    def _tab_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)

    # ------------------------------------------------------------------ UI

    def _build_images_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE.md)
        self.images_table = self._table(
            ["IMAGE / BLUEPRINT", "TARGET", "STATE", "CREATED", "SIZE"],
            (0, 220, 120, 120, 90),
        )
        self.images_table.set_empty_state(
            "No images yet",
            "Build a qcow2 lab image or an installer ISO from a blueprint. "
            "Use New build or Import blueprint above.",
        )
        self.images_table.cellDoubleClicked.connect(
            self._open_selected_image
        )
        self.images_table.itemSelectionChanged.connect(
            self._image_selection_changed
        )
        self.images_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.images_table.customContextMenuRequested.connect(
            self._show_image_menu
        )
        layout.addWidget(self.images_table, 1)

        actions = QHBoxLayout()
        self.open_image_button = QPushButton("Open build log")
        self.open_image_button.setObjectName("SecondaryButton")
        self.open_image_button.setEnabled(False)
        self.open_image_button.clicked.connect(self._open_selected_image_action)
        actions.addWidget(self.open_image_button)
        self.import_template_button = QPushButton("Import as template…")
        self.import_template_button.setObjectName("SecondaryButton")
        self.import_template_button.setEnabled(False)
        self.import_template_button.setToolTip(
            "Import the built qcow2 as a vEMU template, then clone labs from it."
        )
        self.import_template_button.clicked.connect(
            self._import_selected_as_template
        )
        actions.addWidget(self.import_template_button)
        self.reveal_image_button = QPushButton("Reveal in folder")
        self.reveal_image_button.setObjectName("SecondaryButton")
        self.reveal_image_button.setEnabled(False)
        self.reveal_image_button.clicked.connect(
            self._reveal_selected_image
        )
        actions.addWidget(self.reveal_image_button)
        self.delete_image_button = QPushButton("Delete image…")
        self.delete_image_button.setObjectName("SecondaryButton")
        self.delete_image_button.setEnabled(False)
        self.delete_image_button.setToolTip(
            "Delete the built image and its build log. The blueprint is kept."
        )
        self.delete_image_button.clicked.connect(self._delete_selected_image)
        actions.addWidget(self.delete_image_button)
        self.cancel_image_button = QPushButton("Cancel build")
        self.cancel_image_button.setObjectName("DangerButton")
        self.cancel_image_button.setVisible(False)
        self.cancel_image_button.clicked.connect(
            self._cancel_selected_image
        )
        actions.addWidget(self.cancel_image_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        return page

    def _build_blueprints_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE.md)
        self.blueprints_table = self._table(
            ["BLUEPRINT", "VERSION", "DISTRO", "PATH"],
            (0, 90, 150, 0),
        )
        self.blueprints_table.itemSelectionChanged.connect(
            self._blueprint_selection_changed
        )
        self.blueprints_table.set_empty_state(
            "No blueprints imported",
            "Import a native image-builder TOML/JSON blueprint to get "
            "started. The file stays yours; vEMU only keeps a small registry.",
        )
        layout.addWidget(self.blueprints_table, 1)

        actions = QHBoxLayout()
        self.validate_button = QPushButton("Validate")
        self.validate_button.setObjectName("SecondaryButton")
        self.validate_button.setEnabled(False)
        self.validate_button.clicked.connect(self._validate_selected)
        actions.addWidget(self.validate_button)
        self.reveal_button = QPushButton("Reveal in folder")
        self.reveal_button.setObjectName("SecondaryButton")
        self.reveal_button.setEnabled(False)
        self.reveal_button.clicked.connect(self._reveal_selected)
        actions.addWidget(self.reveal_button)
        self.remove_button = QPushButton("Remove…")
        self.remove_button.setObjectName("SecondaryButton")
        self.remove_button.setEnabled(False)
        self.remove_button.setToolTip(
            "Remove the blueprint from Image Builder; the file is kept."
        )
        self.remove_button.clicked.connect(self._remove_selected)
        actions.addWidget(self.remove_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        return page

    def _table(self, headers: list[str], widths: tuple[int, ...]):
        table = EmptyStateTable(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        for column in range(len(headers)):
            item = table.horizontalHeaderItem(column)
            if item is not None:
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter
                )
        table.setItemDelegate(NoFocusDelegate(table))
        table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(36)
        table.horizontalHeader().setFixedHeight(32)
        table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        table.horizontalHeader().setMinimumSectionSize(60)
        for column, width in enumerate(widths):
            if column == 0 or width <= 0:
                continue
            table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Interactive
            )
            table.horizontalHeader().resizeSection(column, width)
        table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setShowGrid(False)
        return table

    # ------------------------------------------------------------- state/UI

    def refresh(self) -> None:
        host = self.adapter.host_status()
        self.host_text.setText(host.summary)
        self.host_text.setToolTip(host.detail)
        if host.ready:
            color = self._tokens.success
        else:
            color = self._tokens.warning
        self.host_dot.setStyleSheet(f"color: {color};")
        self.new_button.setEnabled(host.ready)
        self.new_iso_button.setEnabled(host.ready)
        self.catalog_button.setEnabled(host.ready)
        self.packages_button.setEnabled(host.ready)
        self.recheck_button.setEnabled(True)
        self.import_button.setEnabled(True)
        if not host.ready:
            self.host_notice.setText(
                f"{host.detail} New builds are disabled until the host tools "
                "are installed.".strip()
            )
            self.host_notice.show()
        else:
            self.host_notice.clear()
            self.host_notice.hide()

        self._discover_images()
        self.images = list(self.adapter.images())
        self.blueprints = list(self.adapter.blueprints())
        self._populate_images()
        self._populate_blueprints()
        self._blueprint_selection_changed()
        self.status.emit(
            f"Image Builder · {len(self.images)} image(s) · "
            f"{len(self.blueprints)} blueprint(s)"
        )
        if not self._storage_checked and self.backend is not None:
            if not self._storage_timer.isActive():
                self._storage_timer.start(0)

    def _discover_images(self) -> None:
        """Adopt qcow2 artifacts left by earlier sessions."""

        if not isinstance(self.adapter, RealImageBuilderAdapter):
            return
        root = Path(
            self.adapter.default_output_dir("qcow2")
        ).expanduser()
        if not root.is_dir():
            return
        known = {
            str(Path(image.path).expanduser().resolve())
            for image in self.adapter.images()
        }
        try:
            artifacts = sorted(
                root.glob("*/*.qcow2"),
                key=lambda item: item.stat().st_mtime,
            )
        except OSError:
            return
        for artifact in artifacts:
            if not artifact.is_file():
                continue
            if artifact.name == "disk.qcow2":
                continue
            resolved = str(artifact.resolve())
            if resolved in known:
                continue
            job_dir = artifact.parent
            try:
                stat = artifact.stat()
            except OSError:
                continue
            image = ImageRecord(
                name=self._image_name_from_dir(job_dir.name, artifact.stem),
                target=(
                    f"{self._distro_from_artifact(artifact.name)} / qcow2"
                ),
                state="Succeeded",
                created=datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                size=f"{stat.st_size / (1024 ** 3):.1f} GB",
                path=resolved,
            )
            self.adapter.adopt_image(image)
            known.add(resolved)
            self.build_jobs.append(
                BuildJob(
                    image=image,
                    command=[],
                    log_path=job_dir / "build.log",
                    state="Succeeded",
                )
            )

    @staticmethod
    def _image_name_from_dir(directory: str, fallback: str) -> str:
        parts = directory.rsplit("-", 2)
        base = parts[0] if len(parts) == 3 else directory
        if base.endswith("-qcow2"):
            base = base[: -len("-qcow2")]
        return base or fallback

    @staticmethod
    def _distro_from_artifact(filename: str) -> str:
        stem = filename[:-6] if filename.endswith(".qcow2") else filename
        if "-generic-qcow2-" in stem:
            return stem.split("-generic-qcow2-", 1)[0]
        if "-qcow2-" in stem:
            return stem.split("-qcow2-", 1)[0]
        return stem

    def _populate_images(self) -> None:
        self.images_table.setRowCount(len(self.images))
        for row, image in enumerate(self.images):
            values = [
                image.name,
                image.target,
                image.state,
                image.created,
                image.size,
            ]
            for column, value in enumerate(values):
                self.images_table.setItem(
                    row, column, QTableWidgetItem(str(value))
                )
        self.images_table.refresh_empty_state()
        self._image_selection_changed()

    def _populate_blueprints(self) -> None:
        self.blueprints_table.setRowCount(len(self.blueprints))
        for row, blueprint in enumerate(self.blueprints):
            values = [
                blueprint.name,
                blueprint.version,
                f"{blueprint.distro}"
                + (f" · {blueprint.desktop}" if blueprint.desktop else ""),
                blueprint.path,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, blueprint.path)
                    item.setToolTip(blueprint.path)
                if column == 3:
                    item.setToolTip(blueprint.path)
                self.blueprints_table.setItem(row, column, item)
        self.blueprints_table.refresh_empty_state()
        saved = self._settings.value(self.SELECTED_BLUEPRINT_KEY, "")
        if isinstance(saved, str) and saved:
            for row, blueprint in enumerate(self.blueprints):
                if blueprint.path == saved:
                    self.blueprints_table.selectRow(row)
                    break

    def _stored_blueprint_paths(self) -> list[str]:
        raw = self._settings.value(self.BLUEPRINT_KEY, [])
        if isinstance(raw, str):
            return [raw] if raw else []
        if isinstance(raw, (list, tuple)):
            return [str(item) for item in raw if item]
        return []

    def _restore_blueprints(self) -> None:
        for path in self._stored_blueprint_paths():
            if not Path(path).is_file():
                continue
            try:
                self.adapter.import_blueprint(path)
            except VemuError:
                continue

    def _save_blueprints(self) -> None:
        self._settings.setValue(
            self.BLUEPRINT_KEY,
            [blueprint.path for blueprint in self.adapter.blueprints()],
        )

    def _blueprint_selection_changed(self) -> None:
        blueprint = self._selected_blueprint()
        selected = blueprint is not None
        self.validate_button.setEnabled(selected)
        self.reveal_button.setEnabled(selected)
        self.remove_button.setEnabled(selected)
        if blueprint is not None:
            self._settings.setValue(
                self.SELECTED_BLUEPRINT_KEY, blueprint.path
            )

    def _selected_blueprint(self) -> BlueprintRecord | None:
        row = self.blueprints_table.currentRow()
        if row < 0 or row >= len(self.blueprints):
            return None
        item = self.blueprints_table.item(row, 0)
        if item is None:
            return None
        path = str(item.data(Qt.ItemDataRole.UserRole) or "")
        for blueprint in self.blueprints:
            if blueprint.path == path:
                return blueprint
        return None

    # ------------------------------------------------------------- actions

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
        self.tabs.setCurrentIndex(1)
        self.refresh()
        for row, blueprint in enumerate(self.blueprints):
            if blueprint.path == record.path:
                self.blueprints_table.selectRow(row)
                break
        self._save_blueprints()
        self.status.emit(f"Imported blueprint {record.name}")

    def _open_catalog(self) -> None:
        dialog = OSBuildCatalogDialog(self.adapter, parent=self)
        dialog.exec()

    def _open_packages(self) -> None:
        dialog = ImagePackageLookupDialog(
            self.adapter,
            parent=self,
            blueprint=self._selected_blueprint(),
        )
        dialog.exec()

    def new_build(self) -> None:
        self._new_wizard(BuildWizardDialog, "Image Builder unavailable")

    def new_iso_build(self) -> None:
        self._new_wizard(IsoBuildWizardDialog, "Image Builder unavailable")

    def _new_wizard(self, wizard_type, unavailable_title: str) -> None:
        host = self.adapter.host_status()
        if not host.ready:
            QMessageBox.information(
                self,
                unavailable_title,
                host.detail or host.summary,
            )
            return
        dialog = wizard_type(self.adapter, dark=self.dark, parent=self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        job = dialog.created
        if job is None:
            return
        self.build_jobs.append(job)
        self.refresh()
        self._save_blueprints()
        self.tabs.setCurrentIndex(0)
        self._open_build_window(job)
        self.status.emit(
            f"Build {job.state.lower()}: {job.image.name} "
            f"({job.image.target})"
        )

    def _open_build_window(self, job: BuildJob) -> None:
        key = id(job)
        window = self.build_windows.get(key)
        if window is None:
            window = BuildWindow(job, dark=self.dark)
            window.finished.connect(self._build_finished)
            window.closed.connect(self._build_window_closed)
            window.hidden.connect(
                lambda name: self.status.emit(
                    f"Build window hidden; {name} continues in the background."
                )
            )
            self.build_windows[key] = window
        window.show()
        window.raise_()
        window.activateWindow()

    def _build_window_closed(self, job: BuildJob) -> None:
        self.build_windows.pop(id(job), None)

    def _build_finished(self, job: BuildJob) -> None:
        self.refresh()
        self.status.emit(
            f"Build {job.state.lower()}: {job.image.name}"
        )
        if job.state == "Succeeded" and job.image.path.endswith(".qcow2"):
            self._ensure_images_storage(force=True)
        self._image_selection_changed()

    def _job_for(self, image) -> BuildJob | None:
        return next(
            (job for job in self.build_jobs if job.image is image),
            None,
        )

    def _open_selected_image(self, row: int, _column: int) -> None:
        if row < 0 or row >= len(self.images):
            return
        self._open_image(self.images[row])

    def _open_image(self, image) -> None:
        job = self._job_for(image)
        if job is None:
            self.status.emit(
                "No attached build log for this image in this session."
            )
            return
        self._open_build_window(job)

    def _selected_image(self):
        rows = self.images_table.selectionModel().selectedRows()
        if not rows:
            return None
        row = rows[0].row()
        if row < 0 or row >= len(self.images):
            return None
        return self.images[row]

    def _image_selection_changed(self) -> None:
        image = self._selected_image()
        terminal = image is not None and image.state in self.TERMINAL_STATES
        path = Path(image.path) if image is not None else None
        job = self._job_for(image) if image is not None else None
        self.open_image_button.setEnabled(job is not None)
        self.import_template_button.setEnabled(
            bool(
                terminal
                and path is not None
                and path.suffix.lower() == ".qcow2"
                and path.is_file()
                and self.templates_page is not None
            )
        )
        self.reveal_image_button.setEnabled(
            path is not None and (path.exists() or path.parent.exists())
        )
        self.delete_image_button.setEnabled(terminal)
        running = job is not None and job.state in {"Queued", "Running"}
        self.cancel_image_button.setVisible(running)
        self.cancel_image_button.setEnabled(running)

    def _open_selected_image_action(self) -> None:
        image = self._selected_image()
        if image is not None:
            self._open_image(image)

    def _import_selected_as_template(self) -> None:
        image = self._selected_image()
        if image is None or image.state not in self.TERMINAL_STATES:
            return
        path = Path(image.path)
        if path.suffix.lower() != ".qcow2" or not path.is_file():
            self.status.emit("Only a built qcow2 can become a template.")
            return
        if self.templates_page is None:
            self.status.emit("Templates are not available in this window.")
            return
        created = self.templates_page.import_image(
            str(path), name=image.name
        )
        if created:
            name = str(created.get("name") or image.name)
            self.status.emit(f"Imported {name} as a template")
            self.template_imported.emit(name)

    def _reveal_selected_image(self) -> None:
        image = self._selected_image()
        if image is None:
            return
        path = Path(image.path)
        target = path.parent if path.is_file() else path
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _delete_selected_image(self) -> None:
        image = self._selected_image()
        if image is None or image.state not in self.TERMINAL_STATES:
            return
        job = self._job_for(image)
        iso = image.target.endswith("/ iso")
        if iso:
            question = (
                f"Remove {image.name} from the Images list?\n\n"
                "The ISO file is user-managed and is not deleted."
            )
        else:
            question = (
                f"Delete {image.name}?\n\n"
                "The qcow2 and its build log are removed. "
                "The source blueprint is kept."
            )
        answer = QMessageBox.question(
            self,
            "Delete image",
            question,
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if not iso:
            try:
                self._remove_image_files(job, image)
            except OSError as exc:
                QMessageBox.critical(
                    self,
                    "Delete image",
                    f"Cannot delete the image: {exc}",
                )
                return
        if job is not None:
            window = self.build_windows.pop(id(job), None)
            if window is not None:
                window.shutdown()
            self.build_jobs.remove(job)
        self.adapter.forget_image(image)
        self.refresh()
        self._image_selection_changed()
        self.status.emit(f"Removed {image.name}")

    def _remove_image_files(self, job, image) -> None:
        root = Path(
            self.adapter.default_output_dir("qcow2")
        ).expanduser().resolve()
        artifact = Path(image.path).expanduser()
        if artifact.is_symlink():
            raise OSError(f"Refusing to delete a symlink: {artifact}")
        if artifact.is_file():
            if not self._inside(artifact, root):
                raise OSError(
                    f"Image is outside the managed directory: {artifact}"
                )
            try:
                artifact.unlink()
            except OSError:
                self._delete_managed_path(artifact)
        elif artifact.is_dir() and self._inside(artifact, root):
            try:
                shutil.rmtree(artifact)
            except OSError:
                self._delete_managed_path(artifact)
        if job is not None:
            job_dir = job.log_path.parent.resolve()
            if (
                job_dir != root
                and job_dir != artifact.resolve()
                and self._inside(job_dir, root)
            ):
                try:
                    shutil.rmtree(job_dir)
                except OSError:
                    self._delete_managed_path(job_dir)

    def _delete_managed_path(self, path: Path) -> None:
        """Delete a root-owned qcow2/job directory through the daemon."""
        backend = self.backend
        method = getattr(backend, "storage_delete_file", None)
        if method is None:
            raise OSError(f"Cannot delete {path}: permission denied")
        try:
            method(str(path))
        except (VemuError, OSError) as exc:
            raise OSError(f"Cannot delete {path}: {exc}") from exc

    @staticmethod
    def _inside(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root)
        except ValueError:
            return False
        return True

    def _cancel_selected_image(self) -> None:
        image = self._selected_image()
        if image is None:
            return
        job = self._job_for(image)
        if job is None or job.state not in {"Queued", "Running"}:
            return
        answer = QMessageBox.question(
            self,
            "Cancel build",
            f"Cancel the build for {image.name}? "
            "The log and partial artifacts are kept.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        job.cancel()
        self.status.emit(f"Build cancelled: {image.name}")
        self.refresh()

    def _show_image_menu(self, position) -> None:
        row = self.images_table.rowAt(position.y())
        if row < 0:
            return
        self.images_table.selectRow(row)
        image = self.images[row] if row < len(self.images) else None
        if image is None:
            return
        job = self._job_for(image)
        terminal = image.state in self.TERMINAL_STATES
        menu = QMenu(self)
        open_action = menu.addAction("Open build log")
        open_action.setEnabled(job is not None)
        open_action.triggered.connect(
            lambda: self._open_image(image)
        )
        template_action = menu.addAction("Import as template…")
        template_action.setEnabled(
            self.import_template_button.isEnabled()
        )
        template_action.triggered.connect(self._import_selected_as_template)
        reveal_action = menu.addAction("Reveal in folder")
        reveal_action.setEnabled(self.reveal_image_button.isEnabled())
        reveal_action.triggered.connect(self._reveal_selected_image)
        if job is not None and job.state in {"Queued", "Running"}:
            menu.addSeparator()
            cancel_action = menu.addAction("Cancel build")
            cancel_action.triggered.connect(self._cancel_selected_image)
        if terminal:
            menu.addSeparator()
            delete_action = menu.addAction("Delete image…")
            delete_action.triggered.connect(self._delete_selected_image)
        menu.exec(
            self.images_table.viewport().mapToGlobal(position)
        )

    def _ensure_images_storage(self, *, force: bool = False) -> None:
        if self._storage_checked and not force:
            return
        self._storage_checked = True
        backend = self.backend
        if backend is None:
            return
        if not hasattr(backend, "storage_list") or not hasattr(
            backend, "storage_add"
        ):
            return
        root = Path(
            self.adapter.default_output_dir("qcow2")
        ).expanduser().resolve()
        if not root.is_dir():
            return
        try:
            locations = backend.storage_list()
        except (VemuError, AttributeError):
            return
        for location in locations:
            try:
                path = Path(str(location.get("path", ""))).resolve()
            except (OSError, ValueError):
                continue
            if path == root:
                return
        try:
            backend.storage_add(str(root), "images", dry_run=False)
        except (VemuError, OSError) as exc:
            self.status.emit(f"Could not register image storage: {exc}")
        else:
            self.status.emit(
                f"Registered {root} in Storage (images role)"
            )

    def running_jobs(self) -> list[BuildJob]:
        return [
            job
            for job in self.build_jobs
            if job.state in {"Queued", "Running"}
        ]

    def shutdown(self) -> None:
        self._save_blueprints()
        for window in list(self.build_windows.values()):
            window.shutdown()
        self.build_windows.clear()
        for job in self.build_jobs:
            job.cancel()

    def _validate_selected(self) -> None:
        blueprint = self._selected_blueprint()
        if blueprint is None:
            return
        try:
            issues = self.adapter.validate_blueprint(blueprint.path)
        except VemuError as exc:
            QMessageBox.warning(self, "Validate blueprint", str(exc))
            return
        if issues:
            QMessageBox.warning(
                self, "Validate blueprint", "\n".join(issues)
            )
        else:
            QMessageBox.information(
                self,
                "Validate blueprint",
                f"{blueprint.name} is readable. "
                "Full manifest/package preflight comes with the builder.",
            )

    def _reveal_selected(self) -> None:
        blueprint = self._selected_blueprint()
        if blueprint is None:
            return
        QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(Path(blueprint.path).parent))
        )

    def _remove_selected(self) -> None:
        blueprint = self._selected_blueprint()
        if blueprint is None:
            return
        answer = QMessageBox.question(
            self,
            "Remove blueprint",
            f"Remove {blueprint.name} from Image Builder?\n\n"
            "The blueprint file itself is not deleted.",
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.adapter.remove_blueprint(blueprint.path)
        self.refresh()
        self._save_blueprints()
        self.status.emit(f"Removed blueprint {blueprint.name}")

    def set_theme(self, dark: bool) -> None:
        self.dark = dark
        self._tokens = DARK if dark else LIGHT
        host = self.adapter.host_status()
        if host.ready:
            color = self._tokens.success
        else:
            color = self._tokens.warning
        self.host_dot.setStyleSheet(f"color: {color};")


__all__ = ["ImageBuilderPage"]
