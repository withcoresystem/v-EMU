from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from vemu.errors import VemuError
from vemu.guest.optimize import OptimizationReport
from vemu.guest.transport import CommandResult
from vemu.seamless import prepare as module
from vemu.seamless.prepare import SeamlessPrepare, prepare_seamless


class FakeSSH:
    def __init__(self, results: list[bool] | None = None) -> None:
        self.results = list(results or [True])
        self.calls = 0

    def ping(self) -> bool:
        self.calls += 1
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]

    def run(self, _command: str, *, timeout: int = 60) -> CommandResult:
        ok = self.ping()
        return CommandResult(
            exitcode=0 if ok else 255,
            stdout="ok\n" if ok else "",
            stderr="" if ok else "Permission denied (publickey).",
        )


PREFLIGHT = {
    "supported": True,
    "reasons": [],
    "info": {"edition_id": "Professional", "build": "26300"},
    "services": {"QEMU-GA": {"status": "Running"}},
    "winfsp": {"status": "Running"},
    "ssh_listening": True,
    "rdp_listening": False,
}


class PrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.public_key = self.root / "id.pub"
        self.public_key.write_text("ssh-ed25519 AAAA user@host\n")
        self.key_path = self.root / "id_ed25519"
        self.known_hosts = self.root / "known_hosts"
        self.state_path = self.root / "state.json"
        self.order: list[str] = []
        self.user_hive: str | None = None

    def plan(self, **overrides) -> SeamlessPrepare:
        values = {
            "host": "192.0.2.10",
            "username": "ProUser",
            "domain": "W11",
            "apps": {"Notepad": r"C:\Windows\System32\notepad.exe"},
            "key_path": self.key_path,
            "known_hosts": self.known_hosts,
            "state_path": self.state_path,
        }
        values.update(overrides)
        return SeamlessPrepare(**values)

    def stack(self, *, ssh_results: list[bool] | None = None) -> ExitStack:
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.ssh = FakeSSH(ssh_results)
        original_transport = module.SSHTransport
        module.SSHTransport = lambda *a, **k: self.ssh
        stack.callback(setattr, module, "SSHTransport", original_transport)
        stack.enter_context(
            mock.patch.object(module, "preflight", return_value=PREFLIGHT)
        )
        stack.enter_context(
            mock.patch.object(
                module, "apply_optimization",
                side_effect=self.fake_optimize,
            )
        )
        stack.enter_context(
            mock.patch.object(
                module,
                "apply_overlay",
                side_effect=lambda t: {
                    "hardware_acceleration_disabled": True,
                    "browsers": ["Microsoft Edge"],
                },
            )
        )
        stack.enter_context(
            mock.patch.object(
                module, "ensure_keypair", side_effect=self.fake_keypair
            )
        )
        stack.enter_context(
            mock.patch.object(
                module,
                "inspect_windows_user",
                return_value={
                    "user": "ProUser",
                    "admin": True,
                    "remote_desktop": True,
                    "added_to_remote_desktop": False,
                },
            )
        )
        stack.enter_context(
            mock.patch.object(
                module,
                "install_admin_key",
                side_effect=lambda t, key: self.order.append("key"),
            )
        )
        stack.enter_context(
            mock.patch.object(
                module,
                "scan_host_key",
                side_effect=lambda host, path: self.order.append("scan"),
            )
        )
        stack.enter_context(
            mock.patch.object(
                module, "enable_rdp", side_effect=self.fake_enable_rdp
            )
        )
        stack.enter_context(
            mock.patch.object(
                module,
                "rdp_certificate_sha256",
                return_value="A" * 64,
            )
        )
        stack.enter_context(
            mock.patch.object(
                module,
                "configure_ssh_key_only",
                return_value={
                    "password_authentication": False,
                    "pubkey_authentication": True,
                },
            )
        )
        stack.enter_context(
            mock.patch.object(
                module,
                "ensure_service",
                side_effect=lambda _transport, name, **kwargs: {
                    "name": name,
                    "present": True,
                    "status": "Running",
                    "start_type": "Automatic",
                    "error": "",
                },
            )
        )
        stack.enter_context(
            mock.patch.object(
                module, "pending_reboot", return_value=False
            )
        )
        stack.enter_context(
            mock.patch.object(
                module,
                "add_remoteapp_app",
                side_effect=lambda t, name, path, **kwargs: self.order.append(
                    f"app:{name}"
                ),
            )
        )
        stack.enter_context(
            mock.patch.object(
                module,
                "remoteapp_state",
                return_value={"fDenyTSConnections": 0, "apps": []},
            )
        )
        return stack

    def fake_optimize(self, transport, profile, **kwargs):
        self.order.append(f"optimize:{profile}")
        self.user_hive = kwargs.get("user_hive")
        return OptimizationReport(
            profile=profile,
            exitcode=0,
            log="done",
            state={"active_scheme": {"guid": "g"}},
            problems=(),
        )

    def fake_keypair(self, path):
        self.key_path = path
        return self.public_key

    def fake_enable_rdp(self, transport):
        self.order.append("rdp")
        return {"fDenyTSConnections": 0}

    def test_success_writes_state(self) -> None:
        with self.stack():
            state = prepare_seamless(object(), self.plan())
        self.assertEqual(
            self.order,
            ["optimize:Safe", "key", "scan", "rdp", "app:Notepad"],
        )
        self.assertEqual(state["branch"], "seamless")
        self.assertEqual(state["username"], "ProUser")
        self.assertEqual(state["apps"][0]["name"], "Notepad")
        self.assertEqual(
            state["optimization"], {"active_scheme": {"guid": "g"}}
        )
        self.assertNotIn('"password":', json.dumps(state).lower())
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["domain"], "W11")
        self.assertEqual(saved["key_path"], str(self.key_path))
        mode = os.stat(self.state_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_ssh_timeout_is_retried(self) -> None:
        with self.stack():
            original = self.ssh.run
            raised = {"done": False}

            def run(_command: str, *, timeout: int = 60):
                if not raised["done"]:
                    raised["done"] = True
                    raise VemuError(
                        "SSH command timed out for ProUser@192.0.2.10."
                    )
                return original(_command, timeout=timeout)

            self.ssh.run = run  # type: ignore[method-assign]
            state = prepare_seamless(object(), self.plan())
        self.assertTrue(raised["done"])
        self.assertEqual(state["domain"], "W11")
        self.assertEqual(state["apps"][0]["name"], "Notepad")

    def test_standard_user_profile_is_required(self) -> None:
        with mock.patch.object(
            module, "preflight", return_value=PREFLIGHT
        ), mock.patch.object(
            module,
            "inspect_windows_user",
            return_value={
                "user": "StdUser",
                "admin": False,
                "remote_desktop": True,
                "added_to_remote_desktop": True,
                "profile": "",
            },
        ):
            with self.assertRaises(VemuError) as caught:
                prepare_seamless(object(), self.plan(username="StdUser"))
        self.assertIn("Log into Windows", str(caught.exception))

    def test_preflight_failure_stops_early(self) -> None:
        bad = dict(PREFLIGHT, supported=False, reasons=["Home edition"])
        with mock.patch.object(
            module, "preflight", return_value=bad
        ), mock.patch.object(module, "apply_optimization") as optimize:
            with self.assertRaises(VemuError) as caught:
                prepare_seamless(object(), self.plan())
        self.assertIn("Home edition", str(caught.exception))
        optimize.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_ssh_port_must_be_listening(self) -> None:
        bad = dict(PREFLIGHT, ssh_listening=False)
        with mock.patch.object(module, "preflight", return_value=bad):
            with self.assertRaises(VemuError) as caught:
                prepare_seamless(object(), self.plan())
        self.assertIn("port 22", str(caught.exception))

    def test_optimization_can_be_disabled(self) -> None:
        with self.stack() as stack:
            optimize = stack.enter_context(
                mock.patch.object(module, "apply_optimization")
            )
            state = prepare_seamless(object(), self.plan(optimize=False))
        optimize.assert_not_called()
        self.assertIsNone(state["profile"])
        self.assertIsNone(state["optimization"])

    def test_user_hive_is_passed(self) -> None:
        with self.stack():
            prepare_seamless(
                object(),
                self.plan(user_hive=r"C:\Users\ProUser\NTUSER.DAT"),
            )
        self.assertEqual(self.user_hive, r"C:\Users\ProUser\NTUSER.DAT")

    def test_ssh_retries_then_fails(self) -> None:
        with self.stack(ssh_results=[False]):
            with mock.patch.object(module.time, "sleep") as sleep:
                with self.assertRaises(VemuError) as caught:
                    prepare_seamless(
                        object(), self.plan(ssh_attempts=3, ssh_delay=0)
                    )
        self.assertIn("did not become ready", str(caught.exception))
        self.assertEqual(self.ssh.calls, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse(self.state_path.exists())

    def test_rdp_must_end_enabled(self) -> None:
        with self.stack():
            with mock.patch.object(
                module, "enable_rdp", return_value={"fDenyTSConnections": 1}
            ):
                with self.assertRaises(VemuError) as caught:
                    prepare_seamless(object(), self.plan())
        self.assertIn("still disabled", str(caught.exception))

    def test_unsafe_names_are_rejected(self) -> None:
        with self.assertRaises(VemuError):
            prepare_seamless(object(), self.plan(username="bad/name"))
        with self.assertRaises(VemuError):
            prepare_seamless(object(), self.plan(domain="bad name"))
        with self.assertRaises(VemuError):
            prepare_seamless(object(), self.plan(domain="../x"))
