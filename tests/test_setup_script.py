from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class SetupScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.script = self.root / "setup.sh"

    def test_setup_is_rhel_family_only(self) -> None:
        text = self.script.read_text(encoding="utf-8")
        self.assertIn("Fedora and RHEL-family", text)
        self.assertIn("dnf install -y", text)
        self.assertIn("qemu-system-riscv", text)
        for removed in (
            "apt-get",
            "zypper",
            "pacman",
            "dpkg-query",
            "libvirt-qemu",
        ):
            self.assertNotIn(removed, text)
        for expected in ("/var/lib/vemu/seamless", "/var/lib/vemu/experimental3d"):
            self.assertIn(expected, text)

    def test_setup_syntax(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
