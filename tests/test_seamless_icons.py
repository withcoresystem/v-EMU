from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu.guest.transport import CommandResult
from vemu.seamless import icons

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class FakeTransport:
    def __init__(self, payload: bytes = PNG, *, ok: bool = True) -> None:
        self.payload = payload
        self._ok = ok
        self.scripts: list[str] = []
        self.downloads: list[str] = []

    def run_powershell(self, script: str, *, timeout: int = 120) -> CommandResult:
        self.scripts.append(script)
        return CommandResult(
            exitcode=0 if self._ok else 1, stdout="", stderr=""
        )

    def run(self, *args, **kwargs) -> CommandResult:
        return CommandResult(exitcode=0, stdout="", stderr="")

    def upload(self, data: bytes, remote: str) -> None:
        pass

    def download(self, remote: str, *, max_bytes: int = 0) -> bytes:
        self.downloads.append(remote)
        return self.payload

    def ping(self) -> bool:
        return True


class IconTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = mock.patch.dict(
            "os.environ", {"XDG_DATA_HOME": self.temp.name}
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_extracts_png(self) -> None:
        transport = FakeTransport()
        icon = icons.extract(
            transport,
            r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE",
            "W11",
            "Excel",
        )
        self.assertIsNotNone(icon)
        assert icon is not None
        self.assertTrue(icon.is_file())
        self.assertEqual(icon.read_bytes(), PNG)
        self.assertIn("/", transport.downloads[0])
        self.assertIn("System.Drawing", transport.scripts[0])

    def test_invalid_png_is_ignored(self) -> None:
        transport = FakeTransport(b"not a png")
        self.assertIsNone(
            icons.extract(transport, r"C:\App.exe", "W11", "App")
        )

    def test_extraction_failure_is_ignored(self) -> None:
        transport = FakeTransport(ok=False)
        self.assertIsNone(
            icons.extract(transport, r"C:\App.exe", "W11", "App")
        )

    def test_extracts_store_logo(self) -> None:
        transport = FakeTransport()
        icon = icons.extract_store(
            transport,
            (
                "shell:AppsFolder\\"
                "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
            ),
            "W11",
            "Calculator",
        )
        self.assertIsNotNone(icon)
        self.assertIn("Get-AppxPackageManifest", transport.scripts[0])
        self.assertIn(
            "Microsoft.WindowsCalculator_8wekyb3d8bbwe",
            transport.scripts[0],
        )
        self.assertIn("/", transport.downloads[0])

    def test_store_logo_without_aumid_is_ignored(self) -> None:
        self.assertIsNone(
            icons.extract_store(
                FakeTransport(),
                "--new",
                "W11",
                "Calculator",
            )
        )

    def test_non_exe_is_ignored(self) -> None:
        self.assertIsNone(
            icons.extract(FakeTransport(), r"C:\App.txt", "W11", "App")
        )

    def test_remove(self) -> None:
        icon = icons.extract(
            FakeTransport(), r"C:\App.exe", "W11", "App"
        )
        assert icon is not None
        self.assertTrue(icons.remove("W11", "App"))
        self.assertFalse(icon.exists())
        self.assertFalse(icons.remove("W11", "App"))
