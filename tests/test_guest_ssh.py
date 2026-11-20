from __future__ import annotations

import base64
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu.errors import VemuError
from vemu.guest import ssh
from vemu.guest.ssh import (
    SSHTransport,
    ensure_keypair,
    scan_host_key,
)


def completed(returncode: int = 0, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


class SSHTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.transport = SSHTransport(
            host="192.0.2.10",
            user="ProUser",
            key=Path("/tmp/key"),
            known_hosts=Path("/tmp/known_hosts"),
        )

    def test_base_command_is_key_only_and_pinned(self) -> None:
        command = self.transport._base()
        joined = " ".join(command)
        self.assertIn("BatchMode=yes", joined)
        self.assertIn("IdentitiesOnly=yes", joined)
        self.assertIn("StrictHostKeyChecking=yes", joined)
        self.assertIn("UserKnownHostsFile=/tmp/known_hosts", joined)
        self.assertIn("ProUser@192.0.2.10", joined)
        self.assertNotIn("/p:", joined)
        self.assertNotIn("password", joined.lower())

    def test_run_returns_command_result(self) -> None:
        with mock.patch.object(
            ssh.subprocess, "run", return_value=completed(0, "ok\n", "")
        ):
            result = self.transport.run("echo ok")
        self.assertTrue(result.ok)
        self.assertEqual(result.stdout, "ok\n")

    def test_run_timeout_raises(self) -> None:
        with mock.patch.object(
            ssh.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 1)
        ):
            with self.assertRaises(VemuError):
                self.transport.run("sleep 5")

    def test_sandbox_socket_error_gets_a_hint(self) -> None:
        message = "socket: Address family not supported by protocol"
        with mock.patch.object(
            ssh.subprocess, "run", return_value=completed(255, "", message)
        ):
            result = self.transport.run("echo ok")
        self.assertIn("RestrictAddressFamilies", result.stderr)
        self.assertIn("AF_INET", result.stderr)

    def test_run_powershell_uses_encoded_command(self) -> None:
        with mock.patch.object(
            ssh.subprocess, "run", return_value=completed()
        ) as run:
            self.transport.run_powershell("Get-Process")
        command = run.call_args.args[0]
        remote = command[-1]
        encoded = remote.split("-EncodedCommand ")[1]
        self.assertEqual(
            base64.b64decode(encoded).decode("utf-16le"), "Get-Process"
        )
        self.assertIn("-NonInteractive", remote)

    def test_ping(self) -> None:
        with mock.patch.object(
            ssh.subprocess, "run", return_value=completed(0, "ok\n", "")
        ):
            self.assertTrue(self.transport.ping())
        with mock.patch.object(
            ssh.subprocess, "run", return_value=completed(0, "no\n", "")
        ):
            self.assertFalse(self.transport.ping())

    def test_upload(self) -> None:
        def fake_run(command, **kwargs):
            source = Path(command[-2])
            self.assertEqual(source.read_bytes(), b"payload")
            return completed()

        with mock.patch.object(ssh.subprocess, "run", side_effect=fake_run):
            self.transport.upload(b"payload", r"C:\ProgramData\ssh\keys")

    def test_upload_failure(self) -> None:
        with mock.patch.object(
            ssh.subprocess, "run", return_value=completed(1, "", "denied")
        ):
            with self.assertRaises(VemuError):
                self.transport.upload(b"payload", "remote")

    def test_download(self) -> None:
        def fake_run(command, **kwargs):
            Path(command[-1]).write_bytes(b"contents")
            return completed()

        with mock.patch.object(ssh.subprocess, "run", side_effect=fake_run):
            self.assertEqual(self.transport.download("remote"), b"contents")

    def test_download_limit(self) -> None:
        def fake_run(command, **kwargs):
            Path(command[-1]).write_bytes(b"0123456789")
            return completed()

        with mock.patch.object(ssh.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(VemuError):
                self.transport.download("remote", max_bytes=4)

    def test_download_failure(self) -> None:
        with mock.patch.object(
            ssh.subprocess, "run", return_value=completed(1, "", "denied")
        ):
            with self.assertRaises(VemuError):
                self.transport.download("remote")


class KeyTests(unittest.TestCase):
    def test_ensure_keypair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "id_ed25519"
            pub = ensure_keypair(key)
            self.assertTrue(key.is_file())
            self.assertTrue(pub.is_file())
            self.assertTrue(pub.read_text().startswith("ssh-ed25519 "))
            self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(pub.stat().st_mode), 0o644)

    def test_scan_host_key_writes_known_hosts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            known = Path(tmp) / "known_hosts"
            output = "192.0.2.10 ssh-ed25519 AAAA\n"
            with mock.patch.object(
                ssh.subprocess, "run", return_value=completed(0, output, "")
            ):
                self.assertEqual(scan_host_key("192.0.2.10", known), output)
            self.assertEqual(known.read_text(), output)
            self.assertEqual(stat.S_IMODE(known.stat().st_mode), 0o600)

    def test_scan_host_key_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            known = Path(tmp) / "known_hosts"
            with mock.patch.object(
                ssh.subprocess, "run", return_value=completed(1, "", "refused")
            ):
                with self.assertRaises(VemuError):
                    scan_host_key("192.0.2.10", known)
            self.assertFalse(known.exists())
