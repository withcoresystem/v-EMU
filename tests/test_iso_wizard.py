from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from vemu.imagebuilder import FakeImageBuilderAdapter
from vemu.ui.image_builder_iso_wizard import IsoBuildWizardDialog
from vemu.ui.image_builder_wizard import BuildWizardDialog


class IsoWizardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.qcow2 = Path(self.tmp.name) / "desktop.toml"
        self.qcow2.write_text(
            'name = "Test Desktop"\nversion = "1.0"\n'
            'distro = "fedora-45"\n[[groups]]\nname = "gnome-desktop"\n',
            encoding="utf-8",
        )
        self.iso = Path(self.tmp.name) / "desktop-iso.toml"
        self.iso.write_text(
            'name = "Test Desktop ISO"\nversion = "1.0"\n'
            'distro = "fedora-45"\n[[groups]]\nname = "gnome-desktop"\n'
            '[[packages]]\nname = "python3"\nversion = "*"\n',
            encoding="utf-8",
        )

    def _wait(self, wizard) -> None:
        deadline = time.monotonic() + 5
        while wizard._preflight_worker is not None and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.005)
        self.app.processEvents()

    def test_qcow2_wizard_hides_iso_blueprints(self) -> None:
        adapter = FakeImageBuilderAdapter()
        adapter.import_blueprint(self.qcow2)
        adapter.import_blueprint(self.iso)
        wizard = BuildWizardDialog(adapter)
        try:
            self.assertEqual(
                [item.name for item in wizard.blueprints], ["Test Desktop"]
            )
        finally:
            wizard.done(0)

    def test_iso_wizard_automatic_payload_fields(self) -> None:
        adapter = FakeImageBuilderAdapter()
        adapter.import_blueprint(self.iso)
        wizard = IsoBuildWizardDialog(adapter)
        try:
            wizard._next()
            wizard._next()
            self.assertEqual(wizard.step, 2)
            wizard.firstboot_check.setChecked(True)
            self.assertTrue(wizard.script_name.isEnabled())
            wizard.script_name.setText("site-setup.sh")
            wizard.script_url.setText("https://deploy.example/firstboot/")
            values = wizard._firstboot()
            self.assertIsNotNone(values)
            assert values is not None
            self.assertEqual(values.script_name, "site-setup.sh")
            self.assertEqual(
                values.script_url, "https://deploy.example/firstboot/"
            )

            wizard.script_url.setText("https://deploy.example/site-setup.sh")
            self.assertFalse(wizard._next())
            self.assertEqual(wizard.step, 2)
            self.assertIn("must end with", wizard.firstboot_note.text())

            wizard.firstboot_check.setChecked(False)
            self.assertFalse(wizard.script_name.isEnabled())
        finally:
            wizard.done(0)

    def test_iso_wizard_five_steps(self) -> None:
        adapter = FakeImageBuilderAdapter()
        adapter.import_blueprint(self.iso)
        wizard = IsoBuildWizardDialog(adapter)
        try:
            self.assertEqual(wizard.blueprint.name, "Test Desktop ISO")
            wizard._next()
            self.assertEqual(wizard.step, 1)
            wizard.brand_check.setChecked(True)
            wizard._next()
            self.assertEqual(wizard.step, 2)
            wizard.firstboot_check.setChecked(True)
            wizard._next()
            self.assertEqual(wizard.step, 3)
            self._wait(wizard)
            self.assertIsNotNone(wizard.preflight)
            self.assertFalse(wizard.preflight.blocking)
            wizard._next()
            self.assertEqual(wizard.step, 4)
            self.assertTrue(wizard.output_edit.text())
            wizard.root_check.setChecked(True)
            self.assertTrue(wizard.start_button.isEnabled())
            wizard._start_build()
            self.assertIsNotNone(wizard.created)
            self.assertEqual(wizard.created.image.state, "Queued")
        finally:
            wizard.done(0)


if __name__ == "__main__":
    unittest.main()
