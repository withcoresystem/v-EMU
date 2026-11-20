from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest
from unittest import mock

from vemu.backend import DomainInfo

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_PYSIDE = importlib.util.find_spec("PySide6") is not None


@unittest.skipUnless(HAS_PYSIDE, "PySide6 is not installed")
class SeamlessUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from vemu.ui import configure_webengine_environment

        configure_webengine_environment()
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QApplication

        cls._settings_dir = tempfile.mkdtemp(prefix="vemu-seamless-ui-")
        QSettings.setPath(
            QSettings.Format.NativeFormat,
            QSettings.Scope.UserScope,
            cls._settings_dir,
        )
        cls.addClassCleanup(shutil.rmtree, cls._settings_dir, True)
        cls.app = QApplication.instance() or QApplication([])

    def wait_task(self, task) -> None:
        from PySide6.QtTest import QTest

        self.assertIsNotNone(task)
        self.assertTrue(task.wait(3000))
        for _ in range(50):
            self.app.processEvents()
            QTest.qWait(10)

    def domain(self, name: str, os_type: str, state: str = "running"):
        from vemu.backend import DomainInfo

        return DomainInfo(
            name=name,
            uuid="uuid-" + name,
            state=state,
            disks=["/tmp/disk.qcow2"],
            autostart=False,
            firmware="bios",
            managed_by="v-EMU (full)",
            os_type=os_type,
        )

    def test_context_menu_only_for_windows(self) -> None:
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self):
                return [
                    DomainInfo(
                        name="vm-win",
                        uuid="u-win",
                        state="running",
                        disks=["/d"],
                        autostart=False,
                        firmware="bios",
                        managed_by="v-EMU (full)",
                        os_type="windows",
                    ),
                    DomainInfo(
                        name="vm-linux",
                        uuid="u-linux",
                        state="running",
                        disks=["/d"],
                        autostart=False,
                        firmware="bios",
                        managed_by="v-EMU (full)",
                        os_type="linux",
                    ),
                ]

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)
        try:
            def select(name: str) -> None:
                for row in range(window.table.rowCount()):
                    item = window.table.item(row, 0)
                    if item is not None and item.text() == name:
                        window.table.selectRow(row)
                        return

            select("vm-win")
            menu = window._build_context_menu(window.selected_domain)
            actions = {action.text(): action for action in menu.actions()}
            self.assertIn("Seamless Mode", actions)
            submenu = actions["Seamless Mode"].menu()
            labels = [action.text() for action in submenu.actions()]
            self.assertEqual(
                labels,
                ["Activate Seamless mode…", "Manage Windows programs…"],
            )

            select("vm-linux")
            linux_menu = window._build_context_menu(window.selected_domain)
            self.assertNotIn(
                "Seamless Mode",
                [action.text() for action in linux_menu.actions()],
            )
        finally:
            window.close()

    def test_context_menu_shows_deactivate_when_active(self) -> None:
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self):
                return [
                    DomainInfo(
                        name="vm-win",
                        uuid="u-win",
                        state="running",
                        disks=["/d"],
                        autostart=False,
                        firmware="bios",
                        managed_by="v-EMU (full)",
                        os_type="windows",
                    )
                ]

            def vm_seamless_state(self, _name: str) -> dict:
                return {
                    "domain": "vm-win",
                    "prepared": True,
                    "active": True,
                    "state": {"username": "ProUser", "apps": []},
                }

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)
        try:
            window.table.selectRow(0)
            menu = window._build_context_menu(window.selected_domain)
            actions = {action.text(): action for action in menu.actions()}
            labels = [
                action.text()
                for action in actions["Seamless Mode"].menu().actions()
            ]
            self.assertIn("Disable seamless mode…", labels)
            self.assertNotIn("Activate Seamless mode…", labels)
        finally:
            window.close()

    def test_context_menu_offers_disable_without_checkpoint(self) -> None:
        from vemu.ui.app import MainWindow

        class FakeBackend:
            def list_domains(self):
                return [
                    DomainInfo(
                        name="vm-win",
                        uuid="u-win",
                        state="running",
                        disks=["/d"],
                        autostart=False,
                        firmware="bios",
                        managed_by="v-EMU (full)",
                        os_type="windows",
                    )
                ]

            def vm_seamless_state(self, _name: str) -> dict:
                return {
                    "domain": "vm-win",
                    "prepared": True,
                    "active": True,
                    "state": {"username": "ProUser", "apps": []},
                    "rollback": None,
                }

            def vm_seamless_deactivate(self, *_args, **_kwargs) -> dict:
                return {"removed_apps": []}

            def vm_seamless_rollback(self, *_args, **_kwargs) -> dict:
                return {}

            def close(self) -> None:
                return None

        window = MainWindow(FakeBackend(), dark=False)
        try:
            window.table.selectRow(0)
            menu = window._build_context_menu(window.selected_domain)
            actions = {action.text(): action for action in menu.actions()}
            labels = [
                action.text()
                for action in actions["Seamless Mode"].menu().actions()
            ]
            self.assertIn("Disable seamless mode…", labels)
            self.assertNotIn(
                "Remove seamless host setup (keep Windows)…", labels
            )
            self.assertNotIn(
                "Remove seamless mode and restore checkpoint…", labels
            )
        finally:
            window.close()

    def test_disable_dialog_pause_and_remove_choices(self) -> None:
        from vemu.ui.seamless import DisableSeamlessDialog

        dialog = DisableSeamlessDialog(
            "vm", active=True, checkpoint=True
        )
        try:
            self.assertEqual(dialog.selection()[0], "pause")
            dialog.remove_radio.setChecked(True)
            self.assertFalse(dialog.forget_check.isHidden())
            self.assertEqual(dialog.selection()[0], "remove")
        finally:
            dialog.close()

        no_checkpoint = DisableSeamlessDialog(
            "vm", active=True, checkpoint=False
        )
        try:
            self.assertFalse(no_checkpoint.remove_radio.isEnabled())
            self.assertEqual(no_checkpoint.selection()[0], "pause")
        finally:
            no_checkpoint.close()

        paused = DisableSeamlessDialog(
            "vm", active=False, checkpoint=True
        )
        try:
            self.assertTrue(paused.pause_radio.isHidden())
            self.assertEqual(paused.selection()[0], "remove")
        finally:
            paused.close()

    def backend(self):
        class FakeBackend:
            def __init__(self) -> None:
                self.prepared: list[tuple] = []
                self.video_calls: list[str] = []
                self.available_apps: list[dict] = []
                self.apps = [
                    {
                        "name": "Notepad",
                        "path": "C:\\Windows\\notepad.exe",
                        "arguments": "",
                        "verified": False,
                    }
                ]

            def vm_seamless_status(self, _name: str) -> dict:
                return {
                    "domain": "vm-win",
                    "prepared": bool(self.prepared or self.apps),
                    "running": True,
                    "state": {
                        "username": "ProUser",
                        "profile": "Safe",
                        "active": True,
                        "apps": self.apps,
                        "available_apps": self.available_apps,
                    },
                    "preflight": {"supported": True, "reasons": []},
                }

            def vm_config_info(self, _name: str) -> dict:
                return {"config": {"video": "qxl"}}

            def vm_seamless_prepare(
                self, name, username, profile="Safe", apps=None,
                guest_ip=None, user_hive=None,
            ) -> dict:
                self.prepared.append((name, username, profile))
                return {"domain": name}

            def vm_seamless_video(self, name: str, model: str = "qxl") -> dict:
                self.video_calls.append(model)
                return {"changed": True, "restart_required": False}

            def vm_seamless_remove_app(self, name: str, app: str) -> dict:
                self.apps = [
                    item for item in self.apps if item["name"] != app
                ]
                return {"warning": ""}

            def vm_seamless_refresh_apps(
                self,
                _name: str,
                include_system: bool = False,
                include_store: bool = False,
            ) -> dict:
                return {
                    "available": [
                        {
                            "name": "Notepad",
                            "path": "C:\\Windows\\notepad.exe",
                            "arguments": "",
                        }
                    ],
                    "new": [],
                    "missing": [],
                }

            def vm_seamless_add_app(self, *args, **kwargs) -> dict:
                return {}

        return FakeBackend()

    def test_programs_dialog_hides_store_by_default(self) -> None:
        from vemu.ui.seamless import SeamlessProgramsDialog

        backend = self.backend()
        backend.available_apps = [
            {
                "name": "Notepad",
                "path": "C:\\Windows\\notepad.exe",
                "arguments": "",
                "source": "start-menu",
            },
            {
                "name": "Calculator",
                "path": "C:\\Windows\\explorer.exe",
                "arguments": (
                    "shell:AppsFolder\\"
                    "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
                ),
                "source": "store",
            },
        ]
        dialog = SeamlessProgramsDialog(backend, "vm-win")
        try:
            self.assertFalse(dialog.show_store.isChecked())
            self.assertIn("Store apps hidden", dialog.status.text())
            self.assertEqual(
                [item["name"] for item in dialog.available],
                ["Notepad"],
            )
        finally:
            dialog.close()

    def test_programs_table_marks_store_apps(self) -> None:
        from vemu.ui.seamless import SeamlessProgramsDialog

        backend = self.backend()
        backend.apps.append(
            {
                "name": "Calculator",
                "path": "C:\\Windows\\explorer.exe",
                "arguments": (
                    "shell:AppsFolder\\"
                    "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
                ),
                "verified": True,
            }
        )
        dialog = SeamlessProgramsDialog(backend, "vm-win")
        try:
            paths = {}
            for row in range(dialog.table.rowCount()):
                name = dialog.table.item(row, 0).text()
                paths[name] = dialog.table.item(row, 1).text()
            self.assertEqual(paths["Calculator"], "Store app")
        finally:
            dialog.close()

    def test_activate_dialog(self) -> None:
        from PySide6.QtWidgets import QDialog

        from vemu.ui.seamless import SeamlessActivateDialog

        backend = self.backend()
        dialog = SeamlessActivateDialog(backend, "vm-win")
        try:
            dialog.username.setText("ProUser")
            dialog.password.setText("secret")
            dialog._precheck()
            self.wait_task(dialog._precheck_task)
            self.assertTrue(dialog.checked)
            self.assertTrue(dialog.activate_button.isEnabled())
            dialog.save_password.setChecked(False)
            with mock.patch(
                "vemu.ui.seamless.QMessageBox.information"
            ):
                dialog._activate()
                self.wait_task(dialog._activate_task)
            self.assertTrue(dialog.changed)
            self.assertEqual(dialog.result(), int(QDialog.DialogCode.Accepted))
            self.assertEqual(
                backend.prepared, [("vm-win", "ProUser", "Safe")]
            )
        finally:
            dialog.close()

    def test_activate_dialog_requires_qxl(self) -> None:
        from vemu.ui.seamless import SeamlessActivateDialog

        backend = self.backend()
        backend.vm_config_info = lambda _name: {"config": {"video": "virtio"}}
        dialog = SeamlessActivateDialog(backend, "vm-win")
        try:
            dialog.username.setText("ProUser")
            dialog.password.setText("secret")
            dialog._precheck()
            self.wait_task(dialog._precheck_task)
            self.assertFalse(dialog.checked)
            self.assertFalse(dialog.activate_button.isEnabled())
            self.assertIn("QXL", dialog.status.text())
            self.assertFalse(hasattr(dialog, "video_button"))
        finally:
            dialog.close()

    def test_programs_dialog_lists_and_removes(self) -> None:
        from vemu.ui.seamless import SeamlessProgramsDialog

        backend = self.backend()
        dialog = SeamlessProgramsDialog(backend, "vm-win")
        try:
            self.assertEqual(dialog.table.rowCount(), 1)
            self.assertEqual(dialog.table.item(0, 0).text(), "Notepad")
            dialog.table.selectRow(0)
            with mock.patch(
                "vemu.ui.seamless.QMessageBox.question",
                return_value=__import__(
                    "PySide6.QtWidgets", fromlist=["QMessageBox"]
                ).QMessageBox.StandardButton.Yes,
            ), mock.patch("vemu.ui.seamless.QMessageBox.warning"):
                dialog._remove()
            self.assertEqual(backend.apps, [])
            self.assertEqual(dialog.table.rowCount(), 0)
        finally:
            dialog.close()

    def test_add_dialog_does_not_shadow_qdialog_result(self) -> None:
        from PySide6.QtWidgets import QDialog

        from vemu.ui.seamless import AddProgramDialog

        dialog = AddProgramDialog([])
        try:
            dialog.name.setText("Word")
            dialog.path.setText(
                "C:\\Program Files\\Microsoft Office\\root\\Office16\\WINWORD.EXE"
            )
            dialog._add()
            self.assertEqual(
                dialog.result(), int(QDialog.DialogCode.Accepted)
            )
            self.assertIsNotNone(dialog.selection)
            self.assertEqual(dialog.selection["name"], "Word")
        finally:
            dialog.close()

    def test_test_launch_completion_updates_verified_state(self) -> None:
        from vemu.ui.seamless import SeamlessProgramsDialog

        backend = self.backend()
        dialog = SeamlessProgramsDialog(backend, "vm-win")
        try:
            dialog._test_app_name = "Notepad"
            with mock.patch(
                "vemu.ui.seamless.QMessageBox.information"
            ):
                dialog._test_done({"ok": True}, None)
            self.assertTrue(dialog.changed)
            self.assertTrue(dialog.refresh_button.isEnabled())
        finally:
            dialog.close()

    def test_test_launch_reports_paused_supervisor(self) -> None:
        from vemu.ui.seamless import SeamlessProgramsDialog

        backend = self.backend()
        dialog = SeamlessProgramsDialog(backend, "vm-win")
        try:
            dialog._test_app_name = "Notepad"
            with mock.patch(
                "vemu.ui.seamless.QMessageBox.information"
            ) as info:
                dialog._test_done(
                    {
                        "ok": True,
                        "stopped_other_supervisors": ["vm-other"],
                    },
                    None,
                )
            message = info.call_args.args[2]
            self.assertIn("vm-other", message)
            self.assertIn("Paused the RAIL session", message)
        finally:
            dialog.close()

    def test_refresh_completion_is_applied(self) -> None:
        from vemu.ui.seamless import SeamlessProgramsDialog

        backend = self.backend()
        dialog = SeamlessProgramsDialog(backend, "vm-win")
        try:
            payload = {
                "available": [
                    {
                        "name": "Notepad",
                        "path": "C:\\Windows\\notepad.exe",
                        "arguments": "",
                    }
                ],
                "new": [],
                "missing": [],
            }
            backend.available_apps = list(payload["available"])
            with mock.patch("vemu.ui.seamless.QMessageBox.information"):
                dialog._refresh_done(payload, None)
            self.assertEqual(dialog.available[0]["name"], "Notepad")
            self.assertTrue(dialog.changed)
            self.assertTrue(dialog.refresh_button.isEnabled())
        finally:
            dialog.close()
