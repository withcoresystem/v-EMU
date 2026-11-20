from __future__ import annotations

import unittest

from vemu.guest.transport import CommandResult
from vemu.seamless import store


class FakeTransport:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.scripts: list[str] = []
        self.uploads: list[tuple[bytes, str]] = []

    def run_powershell(self, script: str, *, timeout: int = 120) -> CommandResult:
        self.scripts.append(script)
        text = self.outputs.pop(0) if self.outputs else "0"
        return CommandResult(exitcode=0, stdout=text, stderr="")

    def upload(self, data: bytes, remote: str) -> None:
        self.uploads.append((data, remote))

    def run(self, *args, **kwargs) -> CommandResult:
        return CommandResult(exitcode=0, stdout="", stderr="")

    def download(self, remote: str, *, max_bytes: int = 0) -> bytes:
        return b""

    def ping(self) -> bool:
        return True


class StoreActivatorTests(unittest.TestCase):
    def test_reuses_existing_helper(self) -> None:
        transport = FakeTransport(["4608"])
        self.assertEqual(
            store.ensure_activator(transport),
            store.ACTIVATOR_REMOTE,
        )
        self.assertFalse(transport.uploads)

    def test_compiles_helper_once(self) -> None:
        transport = FakeTransport(["0", "4608"])
        self.assertEqual(
            store.ensure_activator(transport),
            store.ACTIVATOR_REMOTE,
        )
        self.assertEqual(len(transport.uploads), 1)
        self.assertIn(b"IApplicationActivationManager", transport.uploads[0][0])
        self.assertIn("Add-Type", transport.scripts[1])

    def test_compile_failure_falls_back(self) -> None:
        transport = FakeTransport(["0", "0"])
        self.assertIsNone(store.ensure_activator(transport))

    def test_source_contains_com_guids(self) -> None:
        self.assertIn("2e941141-7f97-4756-ba1d-9decde894a3d", store.ACTIVATOR_SOURCE)
        self.assertIn("45BA127D-10A8-46EA-8AB7-56EA9078943C", store.ACTIVATOR_SOURCE)
