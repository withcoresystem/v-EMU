from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QHeaderView, QPlainTextEdit

from vemu.imagebuilder import (
    BuildJob,
    FakeImageBuilderAdapter,
    HostStatus,
    ImageRecord,
    RealImageBuilderAdapter,
)
from vemu.errors import ValidationError
from vemu.ui.build_window import BuildWindow
from vemu.ui.image_builder_page import ImageBuilderPage
from vemu.ui.image_packages import ImagePackageLookupDialog
from vemu.ui.osbuild_catalog import OSBuildCatalogDialog
from vemu.ui.image_builder_wizard import BuildWizardDialog
from vemu.ui.page_tabs import PageTabs


class PageTabsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_switch_and_signal(self) -> None:
        tabs = PageTabs(["Images", "Blueprints"])
        changes: list[int] = []
        tabs.currentChanged.connect(changes.append)
        tabs.setCurrentIndex(1)
        self.assertEqual(tabs.currentIndex(), 1)
        self.assertEqual(changes, [1])
        tabs.setCurrentIndex(0)
        self.assertEqual(tabs.currentIndex(), 0)
        self.assertEqual(changes, [1, 0])

    def test_keyboard_moves_selection(self) -> None:
        tabs = PageTabs(["Images", "Blueprints"])
        tabs.show()
        tabs.buttons[0].setFocus()
        QTest.keyClick(tabs.buttons[0], Qt.Key.Key_Right)
        self.assertEqual(tabs.currentIndex(), 1)
        QTest.keyClick(tabs.buttons[1], Qt.Key.Key_Left)
        self.assertEqual(tabs.currentIndex(), 0)


class _CatalogAdapter(FakeImageBuilderAdapter):
    def catalog(self, *, refresh: bool = False):
        return [
            {
                "distro": "fedora-45",
                "arch": "x86_64",
                "image_type": "ami",
            },
            {
                "distro": "fedora-45",
                "arch": "x86_64",
                "image_type": "generic-qcow2",
            },
            {
                "distro": "fedora-45",
                "arch": "aarch64",
                "image_type": "qcow2",
            },
        ]


class CatalogDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_filters_relevant_types_and_aligns_columns(self) -> None:
        dialog = OSBuildCatalogDialog(_CatalogAdapter())
        QTest.qWait(250)
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.table.item(0, 2).text(), "generic-qcow2")
        alignment = int(dialog.table.horizontalHeader().defaultAlignment())
        self.assertEqual(
            alignment,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
        )
        self.assertEqual(
            int(dialog.table.item(0, 0).textAlignment()),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
        )
        dialog.all_types_check.setChecked(True)
        dialog._populate()
        self.assertEqual(dialog.table.rowCount(), 2)
        dialog.close()

    def test_package_dialog_loads_targets(self) -> None:
        dialog = ImagePackageLookupDialog(_CatalogAdapter())
        self.assertGreater(dialog.distro_combo.count(), 0)
        self.assertTrue(dialog.search_button.isEnabled())
        self.assertEqual(dialog.hidden_check.text(), "Show hidden groups")
        dialog.close()

    def test_group_filter_accepts_commas_and_copy_works(self) -> None:
        dialog = ImagePackageLookupDialog(_CatalogAdapter())
        dialog._groups = [
            {"id": "kde-desktop", "name": "KDE", "installed": "no"},
            {"id": "office", "name": "Office/Productivity", "installed": "no"},
            {"id": "network-server", "name": "Network Servers", "installed": "no"},
        ]
        dialog._groups_loaded = True
        dialog._populate_groups()
        dialog.group_filter.setText("kde, office")
        self.assertEqual(dialog.groups_table.rowCount(), 2)
        dialog._copy("kde-desktop", "group ID")
        self.assertEqual(
            QApplication.clipboard().text(), "kde-desktop"
        )
        dialog.close()


class _MissingToolsAdapter(FakeImageBuilderAdapter):
    def host_status(self) -> HostStatus:
        return HostStatus(
            ready=False,
            summary="Fedora Linux 45 · missing image-builder",
            detail="Install image-builder and osbuild.",
        )


class ImageBuilderPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.blueprint = Path(self.tmp.name) / "desktop.toml"
        self.blueprint.write_text(
            'name = "Test KDE"\n'
            'version = "1.0.0"\n'
            'distro = "fedora-45"\n'
            '[[groups]]\nname = "kde-desktop"\n',
            encoding="utf-8",
        )
        self.settings = mock.patch(
            "vemu.ui.image_builder_page.QSettings",
            return_value=mock.Mock(
                value=lambda *_a, **_k: 0,
                setValue=lambda *_a, **_k: None,
            ),
        )
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def test_empty_page_and_host(self) -> None:
        page = ImageBuilderPage(adapter=FakeImageBuilderAdapter(), dark=True)
        self.assertEqual(page.tabs.currentIndex(), 0)
        self.assertEqual(page.images_table.rowCount(), 0)
        self.assertEqual(page.blueprints_table.rowCount(), 0)
        self.assertTrue(page.new_button.isEnabled())
        self.assertIn("image-builder", page.host_text.text())
        self.assertIn("host checks", page.recheck_button.toolTip())
        self.assertEqual(page.catalog_button.text(), "OSBuild Catalog")
        self.assertIn("image-builder", page.catalog_button.toolTip())
        self.assertEqual(page.packages_button.text(), "Package lookup")
        self.assertIn("package", page.packages_button.toolTip().lower())

    def test_catalog_button_opens_dialog(self) -> None:
        page = ImageBuilderPage(adapter=FakeImageBuilderAdapter(), dark=True)
        with mock.patch(
            "vemu.ui.image_builder_page.OSBuildCatalogDialog"
        ) as dialog_class:
            page._open_catalog()
        dialog_class.assert_called_once()
        dialog_class.return_value.exec.assert_called_once()

    def test_package_lookup_button_opens_dialog(self) -> None:
        page = ImageBuilderPage(adapter=FakeImageBuilderAdapter(), dark=True)
        with mock.patch(
            "vemu.ui.image_builder_page.ImagePackageLookupDialog"
        ) as dialog_class:
            page._open_packages()
        dialog_class.assert_called_once()
        dialog_class.return_value.exec.assert_called_once()

    def test_import_blueprint_appears_in_tab(self) -> None:
        adapter = FakeImageBuilderAdapter()
        record = adapter.import_blueprint(self.blueprint)
        page = ImageBuilderPage(adapter=adapter)
        page.refresh()
        self.assertEqual(page.blueprints_table.rowCount(), 1)
        self.assertEqual(
            page.blueprints_table.item(0, 0).text(), record.name
        )
        page.tabs.setCurrentIndex(1)
        self.assertEqual(page.stack.currentIndex(), 1)
        page.blueprints_table.selectRow(0)
        self.assertTrue(page.validate_button.isEnabled())
        self.assertTrue(page.remove_button.isEnabled())

    def test_missing_tools_keeps_recheck_enabled(self) -> None:
        page = ImageBuilderPage(adapter=_MissingToolsAdapter())
        self.assertFalse(page.new_button.isEnabled())
        self.assertFalse(page.catalog_button.isEnabled())
        self.assertTrue(page.recheck_button.isEnabled())

    def test_tables_allow_column_resize(self) -> None:
        page = ImageBuilderPage(adapter=FakeImageBuilderAdapter())
        for table in (page.images_table, page.blueprints_table):
            header = table.horizontalHeader()
            for column in range(1, table.columnCount()):
                self.assertEqual(
                    header.sectionResizeMode(column),
                    QHeaderView.ResizeMode.Interactive,
                )

    def test_running_image_enables_cancel_button(self) -> None:
        adapter = FakeImageBuilderAdapter()
        record = adapter.import_blueprint(self.blueprint)
        job = adapter.start_build("qcow2", record, self.tmp.name)
        page = ImageBuilderPage(adapter=adapter)
        try:
            page.build_jobs.append(job)
            page.refresh()
            page.images_table.selectRow(0)
            self.assertFalse(page.cancel_image_button.isHidden())
            self.assertTrue(page.cancel_image_button.isEnabled())
        finally:
            page.shutdown()

    def test_succeeded_image_enables_template_action(self) -> None:
        adapter = FakeImageBuilderAdapter()
        record = adapter.import_blueprint(self.blueprint)
        artifact = Path(self.tmp.name) / "disk.qcow2"
        artifact.write_bytes(b"qcow2")
        job = adapter.start_build("qcow2", record, self.tmp.name)
        job.state = "Succeeded"
        job.image.state = "Succeeded"
        job.image.path = str(artifact)
        page = ImageBuilderPage(
            adapter=adapter, templates_page=mock.Mock()
        )
        try:
            page.build_jobs.append(job)
            page.refresh()
            page.images_table.selectRow(0)
            self.assertTrue(page.open_image_button.isEnabled())
            self.assertTrue(page.import_template_button.isEnabled())
            self.assertTrue(page.reveal_image_button.isEnabled())
            self.assertTrue(page.delete_image_button.isEnabled())
            self.assertTrue(page.cancel_image_button.isHidden())
        finally:
            page.shutdown()

    def test_discovers_existing_qcow2_artifacts(self) -> None:
        adapter = RealImageBuilderAdapter()
        root = Path(self.tmp.name) / "images"
        job_dir = root / "vemu-rhel-like-kde-qcow2-20261112-155226"
        job_dir.mkdir(parents=True)
        artifact = job_dir / "almalinux-10.2-qcow2-x86_64.qcow2"
        artifact.write_bytes(b"qcow2")
        (job_dir / "build.log").write_text(
            "source/org.osbuild.librepo: Downloaded Packages/a.rpm\n",
            encoding="utf-8",
        )
        with mock.patch.object(
            adapter, "default_output_dir", return_value=str(root)
        ):
            page = ImageBuilderPage(
                adapter=adapter, templates_page=mock.Mock()
            )
            page.refresh()
        try:
            self.assertEqual(page.images_table.rowCount(), 1)
            self.assertEqual(
                page.images_table.item(0, 0).text(),
                "vemu-rhel-like-kde",
            )
            self.assertEqual(
                page.images_table.item(0, 1).text(),
                "almalinux-10.2 / qcow2",
            )
            page.images_table.selectRow(0)
            self.assertTrue(page.open_image_button.isEnabled())
            self.assertTrue(page.import_template_button.isEnabled())
            self.assertTrue(page.delete_image_button.isEnabled())
        finally:
            page.shutdown()

    def test_registers_images_storage_location(self) -> None:
        adapter = FakeImageBuilderAdapter()
        backend = mock.Mock()
        backend.storage_list.return_value = []
        root = Path(self.tmp.name) / "images"
        root.mkdir()
        page = ImageBuilderPage(adapter=adapter, backend=backend)
        try:
            with mock.patch.object(
                adapter, "default_output_dir", return_value=str(root)
            ):
                page._ensure_images_storage(force=True)
            backend.storage_add.assert_called_once_with(
                str(root), "images", dry_run=False
            )
        finally:
            page.shutdown()

    def test_restores_blueprints_and_selection(self) -> None:
        adapter = FakeImageBuilderAdapter()
        path = str(self.blueprint.resolve())

        class Settings:
            def __init__(self) -> None:
                self.data = {
                    "imagebuilder/blueprints": [path],
                    "imagebuilder/selected_blueprint": path,
                }

            def value(self, key, default=None, type=None):
                return self.data.get(key, default)

            def setValue(self, key, value) -> None:
                self.data[key] = value

        settings = Settings()
        with mock.patch(
            "vemu.ui.image_builder_page.QSettings",
            return_value=settings,
        ):
            page = ImageBuilderPage(adapter=adapter)
            try:
                self.assertEqual(page.blueprints_table.rowCount(), 1)
                self.assertEqual(page.blueprints_table.currentRow(), 0)
            finally:
                page.shutdown()
        self.assertEqual(
            settings.data["imagebuilder/blueprints"], [path]
        )

    def test_theme_update(self) -> None:
        page = ImageBuilderPage(adapter=FakeImageBuilderAdapter(), dark=True)
        page.set_theme(False)
        self.assertFalse(page.dark)

    def test_wizard_fake_preflight_and_build(self) -> None:
        adapter = FakeImageBuilderAdapter()
        record = adapter.import_blueprint(self.blueprint)
        wizard = BuildWizardDialog(adapter)
        self.assertEqual(wizard.step, 0)
        wizard._next()
        self.assertEqual(wizard.step, 1)
        self.assertEqual(wizard.blueprint.name, record.name)
        wizard._next()
        self.assertEqual(wizard.step, 2)
        self.assertFalse(wizard.next_button.isEnabled())
        self._wait_for_preflight(wizard)
        self.assertIsNotNone(wizard.preflight)
        self.assertFalse(wizard.preflight.blocking)
        self.assertTrue(wizard.next_button.isEnabled())
        wizard._next()
        self.assertEqual(wizard.step, 3)
        self.assertTrue(wizard.output_edit.text())
        wizard.root_check.setChecked(True)
        self.assertTrue(wizard.start_button.isEnabled())
        wizard._start_build()
        self.assertIsNotNone(wizard.created)
        self.assertEqual(wizard.created.image.state, "Queued")

    def _wait_for_preflight(self, wizard: BuildWizardDialog) -> None:
        deadline = time.monotonic() + 5
        while (
            wizard._preflight_worker is not None
            and time.monotonic() < deadline
        ):
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()
        self.assertIsNone(wizard._preflight_worker)

    def test_wizard_switches_to_qemu_variant(self) -> None:
        adapter = FakeImageBuilderAdapter()
        adapter.import_blueprint(self.blueprint)
        qemu = self.blueprint.with_name("desktop-qemu.toml")
        qemu.write_text(
            self.blueprint.read_text(encoding="utf-8").replace(
                'name = "Test KDE"', 'name = "Test KDE QEMU"'
            ),
            encoding="utf-8",
        )
        wizard = BuildWizardDialog(adapter)
        try:
            wizard.qemu_check.setChecked(True)
            self.assertEqual(wizard.blueprint.path, str(qemu.resolve()))
            self.assertIn(
                "QEMU-optimized", wizard.profile_note.text()
            )
            wizard.qemu_check.setChecked(False)
            self.assertEqual(
                wizard.blueprint.path, str(self.blueprint.resolve())
            )
            self.assertEqual(wizard.profile_note.text(), "")
        finally:
            wizard.done(0)

    def test_queued_image_appears_in_table(self) -> None:
        adapter = FakeImageBuilderAdapter()
        record = adapter.import_blueprint(self.blueprint)
        adapter.start_build("qcow2", record, self.tmp.name)
        page = ImageBuilderPage(adapter=adapter)
        page.refresh()
        self.assertEqual(page.images_table.rowCount(), 1)
        self.assertEqual(
            page.images_table.item(0, 2).text(), "Queued"
        )

    def test_shutdown_cancels_running_jobs(self) -> None:
        adapter = FakeImageBuilderAdapter()
        record = adapter.import_blueprint(self.blueprint)
        job = adapter.start_build("qcow2", record, self.tmp.name)
        page = ImageBuilderPage(adapter=adapter)
        page.build_jobs.append(job)
        self.assertEqual(page.running_jobs(), [job])
        page.shutdown()
        self.assertEqual(job.image.state, "Cancelled")
        self.assertEqual(page.running_jobs(), [])

    def test_build_window_download_progress(self) -> None:
        log_path = Path(self.tmp.name) / "build.log"
        log_path.write_text(
            "\n".join(
                "source/org.osbuild.librepo: Downloaded Packages/"
                f"pkg{i}.rpm"
                for i in range(3)
            )
            + "\n"
            + "source/org.osbuild.librepo: Already downloaded Packages/"
            "cached0.rpm\n"
            + 'Failed to open file "/sys/fs/selinux/checkreqprot": '
            "Read-only file system\n",
            encoding="utf-8",
        )
        image = ImageRecord(
            name="test",
            target="almalinux-10.2 / qcow2",
            state="Running",
            created="now",
            size="—",
            path=self.tmp.name,
        )
        job = BuildJob(
            image=image,
            command=[],
            log_path=log_path,
            state="Running",
            expected_packages=10,
        )
        window = BuildWindow(job)
        try:
            window._update_log()
            window._update_network()
            self.assertEqual(
                window.log.lineWrapMode(),
                QPlainTextEdit.LineWrapMode.WidgetWidth,
            )
            self.assertIn("4 / 10", window.download_label.text())
            self.assertIn("3 downloaded", window.download_label.text())
            self.assertIn("1 cached", window.download_label.text())
            self.assertEqual(window.download_bar.maximum(), 10)
            self.assertEqual(window.download_bar.value(), 4)
            self.assertFalse(window.download_bar.isHidden())
            self.assertFalse(window.warning_row.isHidden())
            self.assertIn(
                "checkreqprot", window.warning_badge.toolTip()
            )
            self.assertIn("hidden", window.warning_label.text())
            self.assertNotIn(
                "checkreqprot", window.log.toPlainText()
            )
            window._update_log()
            self.assertIn("4 / 10", window.download_label.text())
        finally:
            window.shutdown()

    def test_finished_build_window_actions(self) -> None:
        log_path = Path(self.tmp.name) / "build.log"
        log_path.write_text(
            "source/org.osbuild.librepo: Downloaded Packages/a.rpm\n"
            "source/org.osbuild.librepo: Downloaded Packages/b.rpm\n"
            "source/org.osbuild.librepo: Already downloaded Packages/c.rpm\n"
            "source/org.osbuild.librepo: Already downloaded Packages/d.rpm\n",
            encoding="utf-8",
        )
        image = ImageRecord(
            name="test",
            target="almalinux-10.2 / qcow2",
            state="Succeeded",
            created="now",
            size="1.6 GB",
            path=str(Path(self.tmp.name) / "disk.qcow2"),
        )
        job = BuildJob(
            image=image,
            command=[],
            log_path=log_path,
            state="Succeeded",
            expected_packages=4,
        )
        window = BuildWindow(job)
        try:
            self.assertTrue(window.cancel_button.isHidden())
            self.assertTrue(window.hide_button.isHidden())
            self.assertFalse(window.save_button.isHidden())
            self.assertFalse(window.close_button.isHidden())
            self.assertIn("Built", window.title.text())
            self.assertIn("Build complete", window.download_label.text())
            self.assertIn("4 / 4", window.download_label.text())
            self.assertIn("2 downloaded", window.download_label.text())
            self.assertIn("2 cached", window.download_label.text())
            self.assertEqual(window.download_bar.value(), 4)
        finally:
            window.shutdown()

    def test_preview_build_window(self) -> None:
        adapter = FakeImageBuilderAdapter()
        record = adapter.import_blueprint(self.blueprint)
        job = adapter.start_build("qcow2", record, self.tmp.name)
        window = BuildWindow(job)
        try:
            self.assertIn("Building", window.title.text())
            self.assertEqual(window.status.text(), "Queued (preview)")
            self.assertTrue(window.cancel_button.isEnabled())
            self.assertIn(
                "Preview adapter", window.log.toPlainText()
            )
        finally:
            window.shutdown()


    def test_delete_image_uses_backend_when_local_unlink_fails(self) -> None:
        adapter = FakeImageBuilderAdapter()
        root = Path(self.tmp.name) / "images"
        job_dir = root / "job"
        job_dir.mkdir(parents=True)
        artifact = job_dir / "disk.qcow2"
        artifact.write_bytes(b"qcow2")
        backend = mock.Mock()
        backend.storage_delete_file.return_value = {
            "path": str(artifact),
            "deleted": True,
        }
        job = BuildJob(
            image=ImageRecord(
                name="test",
                target="fedora-45 / qcow2",
                state="Succeeded",
                created="",
                size="",
                path=str(artifact),
            ),
            command=[],
            log_path=job_dir / "build.log",
            state="Succeeded",
        )
        page = ImageBuilderPage(adapter=adapter, backend=backend)
        try:
            with mock.patch.object(
                adapter, "default_output_dir", return_value=str(root)
            ), mock.patch.object(
                Path, "unlink", side_effect=PermissionError("denied")
            ):
                page._remove_image_files(job, job.image)
            backend.storage_delete_file.assert_called_once_with(
                str(artifact)
            )
        finally:
            page.shutdown()


class OSBuildCatalogDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _wait_catalog(self, dialog: OSBuildCatalogDialog) -> None:
        deadline = time.monotonic() + 5
        while dialog._loading and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)

    def test_catalog_lists_entries_and_filters(self) -> None:
        dialog = OSBuildCatalogDialog(FakeImageBuilderAdapter())
        try:
            self._wait_catalog(dialog)
            self.assertEqual(dialog.table.rowCount(), 4)
            self.assertIn("4 of 4", dialog.summary.text())
            dialog.search_edit.setText("almalinux")
            self.assertEqual(dialog.table.rowCount(), 2)
            self.assertIn("2 of 4", dialog.summary.text())
            dialog.search_edit.setText("not-a-real-type")
            self.assertEqual(dialog.table.rowCount(), 0)
            self.assertIn("No build types match", dialog.summary.text())
        finally:
            dialog.close()

    def test_catalog_reports_adapter_error(self) -> None:
        class BrokenAdapter(FakeImageBuilderAdapter):
            def catalog(self, *, refresh: bool = False) -> list[dict[str, str]]:
                raise ValidationError("image-builder was not found")

        dialog = OSBuildCatalogDialog(BrokenAdapter())
        try:
            self._wait_catalog(dialog)
            self.assertEqual(dialog.table.rowCount(), 0)
            self.assertIn("image-builder was not found", dialog.summary.text())
        finally:
            dialog.close()


if __name__ == "__main__":
    unittest.main()
