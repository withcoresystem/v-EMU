from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


class SetupScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[1]
        self.script = self.root / "setup.sh"

    def test_ubuntu_split_riscv_package_is_supported(self) -> None:
        text = self.script.read_text(encoding="utf-8")
        self.assertIn("pkg_available qemu-system-riscv", text)
        self.assertIn("qemu-system-riscv", text)
        for expected in (
            "freerdp3-x11",
            "python3-keyring",
            "/var/lib/vemu/seamless",
            "/var/lib/vemu/experimental3d",
        ):
            self.assertIn(expected, text)
        result = subprocess.run(
            ["bash", "-n", str(self.script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
