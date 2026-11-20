from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu.seamless import shortcuts


class ShortcutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def app(self, name: str = "Word") -> dict:
        return {
            "name": name,
            "path": r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
            "arguments": "",
        }

    def test_install_writes_executable_desktop_file(self) -> None:
        path = shortcuts.install(
            "W11-Seamless",
            self.app(),
            directory=self.directory,
            desktop="kde",
        )
        self.assertEqual(
            path.name, "vemu-seamless-W11-Seamless-Word.desktop"
        )
        text = path.read_text()
        self.assertIn("[Desktop Entry]", text)
        self.assertIn("seamless launch-app W11-Seamless Word", text)
        self.assertIn("Terminal=false", text)
        self.assertNotIn("StartupWMClass", text)
        mode = path.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)

    def test_gnome_adds_startup_wm_class(self) -> None:
        path = shortcuts.install(
            "vm",
            self.app(),
            directory=self.directory,
            desktop="GNOME",
        )
        self.assertIn("StartupWMClass=RAIL", path.read_text())

    def test_kde_omits_startup_wm_class(self) -> None:
        path = shortcuts.install(
            "vm",
            self.app(),
            directory=self.directory,
            desktop="KDE",
        )
        self.assertNotIn("StartupWMClass", path.read_text())

    def test_repair_removes_stale_rail_class_across_domains(self) -> None:
        first = shortcuts.install(
            "vm1",
            self.app("Excel"),
            directory=self.directory,
            desktop="GNOME",
        )
        second = shortcuts.install(
            "vm2",
            self.app("Notepad++"),
            directory=self.directory,
            desktop="GNOME",
        )
        foreign = self.directory / "other.desktop"
        foreign.write_text(
            "[Desktop Entry]\nStartupWMClass=RAIL\n", encoding="utf-8"
        )
        repaired = shortcuts.repair(
            directory=self.directory, desktop="KDE"
        )
        self.assertEqual(set(repaired), {first, second})
        for path in (first, second):
            text = path.read_text()
            self.assertNotIn("StartupWMClass", text)
            self.assertIn("[Desktop Entry]", text)
            self.assertIn("X-vEMU-Seamless-Domain=", text)
        self.assertIn("StartupWMClass=RAIL", foreign.read_text())

    def test_repair_keeps_gnome_shortcut(self) -> None:
        path = shortcuts.install(
            "vm",
            self.app(),
            directory=self.directory,
            desktop="GNOME",
        )
        self.assertEqual(
            shortcuts.repair(directory=self.directory, desktop="GNOME"),
            [],
        )
        self.assertIn("StartupWMClass=RAIL", path.read_text())

    def test_repair_is_idempotent(self) -> None:
        shortcuts.install(
            "vm",
            self.app(),
            directory=self.directory,
            desktop="GNOME",
        )
        shortcuts.repair(directory=self.directory, desktop="KDE")
        self.assertEqual(
            shortcuts.repair(directory=self.directory, desktop="KDE"),
            [],
        )

    def test_name_with_spaces_is_quoted(self) -> None:
        path = shortcuts.install(
            "W11-Seamless",
            self.app("Outlook (classic)"),
            directory=self.directory,
        )
        self.assertIn('"Outlook (classic)"', path.read_text())

    def test_absolute_icon_path_is_used(self) -> None:
        icon = self.directory / "app.png"
        icon.write_bytes(b"png")
        app = self.app()
        app["icon"] = str(icon)
        path = shortcuts.install(
            "vm", app, directory=self.directory
        )
        self.assertIn(f"Icon={icon}", path.read_text())

    def test_missing_icon_falls_back(self) -> None:
        app = self.app()
        app["icon"] = str(self.directory / "missing.png")
        path = shortcuts.install("vm", app, directory=self.directory)
        self.assertIn("Icon=applications-windows", path.read_text())

    def test_filename_is_sanitized(self) -> None:
        path = shortcuts.install(
            "W11/Seamless",
            self.app("My App!"),
            directory=self.directory,
        )
        self.assertEqual(
            path.name, "vemu-seamless-W11-Seamless-My-App.desktop"
        )

    def test_plus_names_do_not_collide(self) -> None:
        path = shortcuts.install(
            "vm", self.app("Notepad++"), directory=self.directory
        )
        self.assertEqual(
            path.name, "vemu-seamless-vm-Notepad++.desktop"
        )

    def test_remove(self) -> None:
        shortcuts.install("vm", self.app(), directory=self.directory)
        self.assertTrue(
            shortcuts.remove("vm", "Word", directory=self.directory)
        )
        self.assertFalse(
            shortcuts.remove("vm", "Word", directory=self.directory)
        )

    def test_remove_all_for_one_domain(self) -> None:
        shortcuts.install("vm1", self.app("Word"), directory=self.directory)
        shortcuts.install("vm1", self.app("Excel"), directory=self.directory)
        shortcuts.install("vm2", self.app("Word"), directory=self.directory)
        removed = shortcuts.remove_all("vm1", directory=self.directory)
        self.assertEqual(len(removed), 2)
        self.assertTrue(
            shortcuts.shortcut_path(
                "vm2", "Word", self.directory
            ).is_file()
        )

    def test_requires_name_and_path(self) -> None:
        from vemu.errors import VemuError

        with self.assertRaises(VemuError):
            shortcuts.install("vm", {"name": "", "path": "x"}, directory=self.directory)
        with self.assertRaises(VemuError):
            shortcuts.install("vm", {"name": "x", "path": ""}, directory=self.directory)

    def test_default_application_dir_honors_xdg(self) -> None:
        with mock.patch.dict(
            os.environ, {"XDG_DATA_HOME": self.temp.name}
        ):
            self.assertEqual(
                shortcuts.applications_dir(),
                Path(self.temp.name) / "applications",
            )
