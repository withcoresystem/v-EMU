from __future__ import annotations

import base64
import unittest
from unittest import mock

from vemu.errors import VemuError
from vemu.guest import qga


class FakeBackend:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.commands: list[dict] = []
        self.exec_status = {
            "exited": True,
            "exitcode": 0,
            "out-data": base64.b64encode(b"hello\n").decode("ascii"),
            "err-data": base64.b64encode(b"oops").decode("ascii"),
        }
        self.pid = 7

    def qemu_agent_command(
        self, domain: str, command: dict, *, timeout: int = 30
    ) -> object:
        self.commands.append(command)
        execute = command.get("execute")
        arguments = command.get("arguments", {})
        if execute == "guest-ping":
            return {}
        if execute == "guest-exec":
            return {"pid": self.pid}
        if execute == "guest-exec-status":
            return dict(self.exec_status)
        if execute == "guest-file-open":
            return 11
        if execute == "guest-file-write":
            handle = str(arguments["handle"])
            self.files.setdefault(handle, b"")
            self.files[handle] += base64.b64decode(arguments["buf-b64"])
            return {"count": arguments["count"]}
        if execute == "guest-file-read":
            handle = str(arguments["handle"])
            data = self.files.get(handle, b"")
            chunk = data[: int(arguments["count"])]
            self.files[handle] = data[len(chunk) :]
            return {
                "buf-b64": base64.b64encode(chunk).decode("ascii"),
                "eof": not self.files[handle],
            }
        if execute == "guest-file-close":
            return {}
        raise AssertionError(f"unexpected command: {execute}")


class QGATransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend()
        self.transport = qga.QGATransport(self.backend, "vm1")

    def test_ping(self) -> None:
        self.assertTrue(self.transport.ping())

    def test_ping_returns_false_on_error(self) -> None:
        def fail(*args, **kwargs):
            raise VemuError("agent down")

        with mock.patch.object(self.backend, "qemu_agent_command", fail):
            self.assertFalse(self.transport.ping())

    def test_run_returns_decoded_output(self) -> None:
        result = self.transport.run(r"C:\Windows\System32\cmd.exe", ["/c", "x"])
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "hello\n")
        self.assertEqual(result.stderr, "oops")
        self.assertEqual(result.output, "hello\n")

    def test_run_rejects_missing_pid(self) -> None:
        self.backend.qemu_agent_command = lambda *a, **k: {}
        with self.assertRaises(VemuError):
            self.transport.run("x")

    def test_run_times_out(self) -> None:
        self.backend.exec_status = {"exited": False}
        with mock.patch.object(qga.time, "monotonic", side_effect=[0, 0, 2]):
            with mock.patch.object(qga.time, "sleep"):
                with self.assertRaises(VemuError):
                    self.transport.run("x", timeout=1)

    def test_run_powershell_encodes_script(self) -> None:
        self.transport.run_powershell("Write-Output 1")
        command = self.backend.commands[-2]
        args = command["arguments"]["arg"]
        self.assertIn("-EncodedCommand", args)
        encoded = args[args.index("-EncodedCommand") + 1]
        self.assertEqual(
            base64.b64decode(encoded).decode("utf-16le"), "Write-Output 1"
        )

    def test_upload_and_download(self) -> None:
        payload = b"x" * (qga.CHUNK + 10)
        self.transport.upload(payload, "file")
        # The fake backend keys files by open handle, not path.
        stored = b"".join(self.backend.files.values())
        self.assertEqual(stored, payload)

    def test_download_returns_uploaded_bytes(self) -> None:
        self.transport.upload(b"data", "file")
        self.assertEqual(self.transport.download("file"), b"data")

    def test_download_limit(self) -> None:
        self.transport.upload(b"0123456789", "file")
        with self.assertRaises(VemuError):
            self.transport.download("file", max_bytes=4)

    def test_text_helpers(self) -> None:
        self.transport.write_text("héllo", "file")
        self.assertEqual(self.transport.read_text("file"), "héllo")

    def test_parse_json(self) -> None:
        result = qga.CommandResult(exitcode=0, stdout='{"a": 1}', stderr="")
        self.assertEqual(qga.QGATransport.parse_json(result), {"a": 1})
