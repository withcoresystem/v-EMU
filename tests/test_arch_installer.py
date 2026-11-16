from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class ArchInstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.script = self.root / "contrib" / "setup-arch.sh"

    def test_script_is_executable_and_valid_shell(self) -> None:
        self.assertTrue(self.script.is_file())
        self.assertTrue(self.script.stat().st_mode & 0o111)
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_script_covers_arch_install_surface(self) -> None:
        text = self.script.read_text(encoding="utf-8")
        for expected in (
            "--precheck",
            "--full",
            "--uninstall",
            "pacman -S",
            "libvirt-qemu",
            "vemud.service",
            "QEMU_EFI-pflash.raw",
            "Image Builder",
        ):
            self.assertIn(expected, text)
        self.assertIn("/usr/lib/virtiofsd", (
            self.root / "setup.sh"
        ).read_text(encoding="utf-8"))
        self.assertIn(
            "contrib",
            (self.root / "packaging" / "build-run.sh").read_text(
                encoding="utf-8"
            ),
        )


if __name__ == "__main__":
    unittest.main()
