from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu.errors import ValidationError
from vemu.external_vnc import (
    ViewerManager,
    find_vncviewer,
    viewer_address,
    viewer_command,
    viewer_endpoint,
)


class ExternalVncCommandTests(unittest.TestCase):
    def test_command_shape(self) -> None:
        command = viewer_command(
            "/usr/bin/vncviewer", {"host": "127.0.0.1", "port": 5900}
        )
        self.assertEqual(
            command,
            [
                "/usr/bin/vncviewer",
                "-Shared=1",
                "-ViewOnly=0",
                "-SendClipboard=0",
                "-AcceptClipboard=0",
                "-SendPrimary=0",
                "-SetPrimary=0",
                "-RemoteResize=0",
                "-AlertOnFatalError=1",
                "-ReconnectOnError=1",
                "127.0.0.1::5900",
            ],
        )

    def test_loopback_endpoints(self) -> None:
        self.assertEqual(
            viewer_endpoint({"host": "127.0.0.1", "port": 5900}),
            ("127.0.0.1", 5900),
        )
        self.assertEqual(
            viewer_endpoint({"host": "::1", "port": 5901}),
            ("::1", 5901),
        )
        self.assertEqual(
            viewer_endpoint({"host": "localhost", "port": 5902}),
            ("127.0.0.1", 5902),
        )
        self.assertEqual(
            viewer_endpoint({"port": 5903}),
            ("127.0.0.1", 5903),
        )

    def test_non_loopback_endpoints_are_refused(self) -> None:
        for host in ("0.0.0.0", "192.168.1.20", "example.com", "::"):
            with self.subTest(host=host):
                with self.assertRaises(ValidationError):
                    viewer_endpoint({"host": host, "port": 5900})

    def test_port_validation(self) -> None:
        for port in (0, 65536, "invalid", None):
            with self.subTest(port=port):
                with self.assertRaises(ValidationError):
                    viewer_endpoint({"host": "127.0.0.1", "port": port})

    def test_ipv6_address(self) -> None:
        self.assertEqual(viewer_address("::1", 5900), "[::1]::5900")

    def test_find_vncviewer(self) -> None:
        with mock.patch(
            "vemu.external_vnc.shutil.which", return_value="/usr/bin/vncviewer"
        ):
            self.assertEqual(find_vncviewer(), "/usr/bin/vncviewer")
        with mock.patch("vemu.external_vnc.shutil.which", return_value=None):
            self.assertIsNone(find_vncviewer())


class FakeProcess:
    def __init__(self, command, **_kwargs) -> None:
        self.command = command
        self.kwargs = _kwargs
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class ExternalVncProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instances: list[FakeProcess] = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = Path(self.tmp.name) / "viewer-vm1.log"

        def factory(command, **kwargs):
            process = FakeProcess(command, **kwargs)
            self.instances.append(process)
            return process

        self.popen = mock.patch(
            "vemu.external_vnc.subprocess.Popen", side_effect=factory
        )
        self.popen.start()
        self.addCleanup(self.popen.stop)
        self.log = mock.patch(
            "vemu.external_vnc.viewer_log_path", return_value=self.log_path
        )
        self.log.start()
        self.addCleanup(self.log.stop)

    def test_missing_viewer(self) -> None:
        with mock.patch(
            "vemu.external_vnc.find_vncviewer", return_value=None
        ):
            with self.assertRaises(ValidationError) as ctx:
                ViewerManager().launch(
                    "vm1", {"host": "127.0.0.1", "port": 5900}
                )
        self.assertIn("TigerVNC vncviewer was not found", str(ctx.exception))

    def test_duplicate_launch_is_refused(self) -> None:
        manager = ViewerManager()
        handle = manager.launch(
            "vm1",
            {"host": "127.0.0.1", "port": 5900},
            viewer="/usr/bin/vncviewer",
            settle_seconds=0,
        )
        self.assertTrue(manager.running("vm1"))
        self.assertIs(manager.handle("vm1"), handle)
        with self.assertRaises(ValidationError) as ctx:
            manager.launch(
                "vm1",
                {"host": "127.0.0.1", "port": 5900},
                viewer="/usr/bin/vncviewer",
                settle_seconds=0,
            )
        self.assertIn("already open", str(ctx.exception))
        handle.process.returncode = 0
        self.assertFalse(manager.running("vm1"))
        manager.launch(
            "vm1",
            {"host": "127.0.0.1", "port": 5900},
            viewer="/usr/bin/vncviewer",
            settle_seconds=0,
        )

    def test_immediate_exit_reports_log(self) -> None:
        manager = ViewerManager()
        original = self.instances

        def failing_factory(command, **kwargs):
            process = FakeProcess(command, **kwargs)
            process.returncode = 2
            original.append(process)
            return process

        with mock.patch(
            "vemu.external_vnc.subprocess.Popen",
            side_effect=failing_factory,
        ):
            with self.assertRaises(ValidationError) as ctx:
                manager.launch(
                    "vm1",
                    {"host": "127.0.0.1", "port": 5900},
                    viewer="/usr/bin/vncviewer",
                    settle_seconds=0,
                )
        self.assertIn("exited immediately", str(ctx.exception))
        self.assertIn(str(self.log_path), str(ctx.exception))
        self.assertFalse(manager.running("vm1"))

    def test_start_failure_is_actionable(self) -> None:
        manager = ViewerManager()
        with mock.patch(
            "vemu.external_vnc.subprocess.Popen",
            side_effect=OSError("cannot execute"),
        ):
            with self.assertRaises(ValidationError) as ctx:
                manager.launch(
                    "vm1",
                    {"host": "127.0.0.1", "port": 5900},
                    viewer="/usr/bin/vncviewer",
                    settle_seconds=0,
                )
        self.assertIn("Could not start TigerVNC", str(ctx.exception))
        self.assertFalse(manager.running("vm1"))

    def test_log_contains_fixed_flags(self) -> None:
        ViewerManager().launch(
            "vm1",
            {"host": "127.0.0.1", "port": 5900},
            viewer="/usr/bin/vncviewer",
            settle_seconds=0,
        )
        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("-Shared=1", text)
        self.assertIn("-ViewOnly=0", text)
        self.assertIn("-SendClipboard=0", text)
        self.assertIn("-AcceptClipboard=0", text)
        self.assertIn("-RemoteResize=0", text)
        self.assertIn("127.0.0.1::5900", text)


class ExternalVncSettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        cls.app = QApplication.instance() or QApplication([])

    def test_external_mode_persists_and_emits(self) -> None:
        from PySide6.QtCore import QSettings as RealQSettings
        from PySide6.QtWidgets import QMessageBox

        from vemu.ui.settings_page import SettingsPage

        class Backend:
            uri = "qemu:///system"

        with tempfile.TemporaryDirectory() as tmp:
            ini = Path(tmp) / "settings.ini"

            def factory(*_args, **_kwargs):
                return RealQSettings(
                    str(ini), RealQSettings.Format.IniFormat
                )

            with mock.patch(
                "vemu.ui.settings_page.QSettings", side_effect=factory
            ), mock.patch(
                "vemu.ui.settings_page.find_vncviewer",
                return_value="/usr/bin/vncviewer",
            ), mock.patch(
                "vemu.ui.settings_page.QMessageBox.question",
                return_value=QMessageBox.StandardButton.Yes,
            ):
                page = SettingsPage(Backend())
                modes: list[str] = []
                page.console_mode_changed.connect(modes.append)
                page.console_mode_combo.setCurrentIndex(
                    page.console_mode_combo.findData("external")
                )
                page._settings.sync()
                self.assertEqual(
                    page._settings.value("ui/console_mode"), "external"
                )
                self.assertEqual(modes, ["external"])

                reopened = SettingsPage(Backend())
                self.assertEqual(
                    reopened.console_mode_combo.currentData(), "external"
                )
                page.deleteLater()
                reopened.deleteLater()

    def test_missing_viewer_disables_external_option(self) -> None:
        from PySide6.QtCore import QSettings as RealQSettings

        from vemu.ui.settings_page import SettingsPage

        class Backend:
            uri = "qemu:///system"

        with tempfile.TemporaryDirectory() as tmp:
            ini = Path(tmp) / "settings.ini"

            def factory(*_args, **_kwargs):
                return RealQSettings(
                    str(ini), RealQSettings.Format.IniFormat
                )

            with mock.patch(
                "vemu.ui.settings_page.QSettings", side_effect=factory
            ), mock.patch(
                "vemu.ui.settings_page.find_vncviewer", return_value=None
            ):
                page = SettingsPage(Backend())
                item = page.console_mode_combo.model().item(
                    page._console_external_index
                )
                self.assertIsNotNone(item)
                self.assertFalse(item.isEnabled())
                self.assertIn(
                    "tigervnc-viewer", page.console_mode_note.text()
                )
                page.deleteLater()


if __name__ == "__main__":
    unittest.main()
