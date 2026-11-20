from __future__ import annotations

import json
import unittest
from typing import Callable

from vemu.errors import VemuError
from vemu.guest.transport import CommandResult
from vemu.seamless.rdp import (
    add_remoteapp_app,
    discover_apps,
    enable_rdp,
    install_admin_key,
    preflight,
    remoteapp_state,
)


def ok(payload: object) -> CommandResult:
    return CommandResult(
        exitcode=0, stdout=json.dumps(payload), stderr=""
    )


class FakeTransport:
    def __init__(self, handler: Callable[[str], CommandResult]) -> None:
        self.handler = handler
        self.scripts: list[str] = []
        self.uploads: list[tuple[bytes, str]] = []
        self.runs: list[tuple[str, tuple[str, ...]]] = []
        self.existing_keys = b""

    def run_powershell(self, script: str, *, timeout: int = 120) -> CommandResult:
        self.scripts.append(script)
        return self.handler(script)

    def run(self, path, args=(), *, timeout: int = 30) -> CommandResult:
        self.runs.append((path, tuple(args)))
        return CommandResult(exitcode=0, stdout="", stderr="")

    def upload(self, data: bytes, remote: str) -> None:
        self.uploads.append((data, remote))

    def download(self, remote: str, *, max_bytes: int = 0) -> bytes:
        return self.existing_keys

    def ping(self) -> bool:
        return True


def info_payload(edition: str = "Professional") -> dict:
    return {
        "product_name": "Windows 11 Pro",
        "display_version": "26H2",
        "build": "26300",
        "ubr": 9711,
        "edition_id": edition,
        "installation_type": "Client",
        "hostname": "LAB",
        "manufacturer": "CoreSystem",
        "model": "vEMU",
    }


def services_payload(sshd: str = "Running") -> dict:
    return {
        "QEMU-GA": {
            "present": True,
            "status": "Running",
            "start_type": "Automatic",
        },
        "sshd": {
            "present": True,
            "status": sshd,
            "start_type": "Automatic",
        },
        "TermService": {
            "present": True,
            "status": "Running",
            "start_type": "Automatic",
        },
        "UmRdpService": {
            "present": True,
            "status": "Running",
            "start_type": "Manual",
        },
        "WinFsp.Launcher": {
            "present": True,
            "status": "Running",
            "start_type": "Automatic",
        },
    }


def rdp_payload() -> dict:
    return {
        "fDenyTSConnections": 0,
        "fDisabledAllowList": 0,
        "apps": [],
        "listeners": [
            {"LocalAddress": "0.0.0.0", "LocalPort": 22},
            {"LocalAddress": "0.0.0.0", "LocalPort": 3389},
        ],
        "firewall": [],
    }


class PreflightTests(unittest.TestCase):
    def transport(self, *, edition="Professional", sshd="Running") -> FakeTransport:
        def handler(script: str) -> CommandResult:
            if "Win32_ComputerSystem" in script:
                return ok(info_payload(edition))
            if "Get-Service -Name" in script:
                return ok(services_payload(sshd))
            if "TSAppAllowList" in script:
                return ok(rdp_payload())
            raise AssertionError(script)

        return FakeTransport(handler)

    def test_supported(self) -> None:
        report = preflight(self.transport())
        self.assertTrue(report["supported"])
        self.assertEqual(report["reasons"], [])
        self.assertTrue(report["ssh_listening"])
        self.assertTrue(report["rdp_listening"])
        self.assertEqual(report["services"]["QEMU-GA"]["status"], "Running")

    def test_unsupported_edition(self) -> None:
        report = preflight(self.transport(edition="Core"))
        self.assertFalse(report["supported"])
        self.assertIn("does not support", report["reasons"][0])

    def test_ssh_not_running(self) -> None:
        report = preflight(self.transport(sshd="Stopped"))
        self.assertFalse(report["supported"])
        self.assertFalse(report["ssh_ready"])
        self.assertTrue(
            any("OpenSSH" in reason for reason in report["reasons"])
        )

    def test_uses_supplied_info(self) -> None:
        from vemu.guest.windows import WindowsInfo

        info = WindowsInfo(
            product_name="Windows 11 Pro",
            display_version="24H2",
            build="26100",
            ubr=1,
            edition_id="Professional",
            installation_type="Client",
            hostname="LAB",
            manufacturer="",
            model="",
        )
        report = preflight(self.transport(), info=info)
        self.assertEqual(report["info"]["build"], "26100")


class RdpSetupTests(unittest.TestCase):
    def test_enable_rdp_script(self) -> None:
        transport = FakeTransport(lambda script: ok(rdp_payload()))
        state = enable_rdp(transport)
        self.assertEqual(state["fDenyTSConnections"], 0)
        script = transport.scripts[0]
        self.assertIn("fDenyTSConnections -Value 0", script)
        self.assertIn("Set-Service -Name TermService -StartupType Automatic", script)
        self.assertIn("Set-NetFirewallRule -Enabled True -Profile Any", script)

    def test_enable_rdp_failure(self) -> None:
        transport = FakeTransport(
            lambda script: CommandResult(exitcode=1, stdout="", stderr="denied")
        )
        with self.assertRaises(VemuError):
            enable_rdp(transport)

    def test_remoteapp_state(self) -> None:
        transport = FakeTransport(lambda script: ok(rdp_payload()))
        self.assertEqual(remoteapp_state(transport)["fDisabledAllowList"], 0)

    def test_add_app_escapes_values(self) -> None:
        payload = {"Name": "O'Brien", "Path": "C:\\App.exe"}
        transport = FakeTransport(lambda script: ok(payload))
        add_remoteapp_app(transport, "O'Brien", r"C:\App's.exe")
        script = transport.scripts[0]
        self.assertIn("'O''Brien'", script)
        self.assertIn(r"C:\App''s.exe", script)
        self.assertIn("TSAppAllowList", script)
        self.assertIn("fDisabledAllowList", script)

    def test_add_app_requires_values(self) -> None:
        transport = FakeTransport(lambda script: ok({}))
        with self.assertRaises(VemuError):
            add_remoteapp_app(transport, "", r"C:\App.exe")
        with self.assertRaises(VemuError):
            add_remoteapp_app(transport, "App", " ")

    def test_add_app_failure(self) -> None:
        transport = FakeTransport(
            lambda script: CommandResult(exitcode=1, stdout="", stderr="denied")
        )
        with self.assertRaises(VemuError):
            add_remoteapp_app(transport, "App", r"C:\App.exe")

    def test_discover_apps(self) -> None:
        transport = FakeTransport(
            lambda script: ok(
                [{"Name": "Notepad", "AppID": "notepad.exe"}]
            )
        )
        apps = discover_apps(transport)
        self.assertEqual(apps[0]["name"], "Notepad")
        self.assertEqual(apps[0]["app_id"], "notepad.exe")

    def test_discover_apps_single_object(self) -> None:
        transport = FakeTransport(
            lambda script: ok({"Name": "Notepad", "AppID": "notepad.exe"})
        )
        self.assertEqual(len(discover_apps(transport)), 1)


class AdminKeyTests(unittest.TestCase):
    KEY = "ssh-ed25519 AAAAC3Nza user@host"

    def test_installs_key_and_restarts_sshd(self) -> None:
        transport = FakeTransport(
            lambda script: CommandResult(exitcode=0, stdout="", stderr="")
        )
        result = install_admin_key(transport, self.KEY)
        self.assertTrue(result["installed"])
        self.assertEqual(transport.uploads[0][1], (
            r"C:\ProgramData\ssh\administrators_authorized_keys"
        ))
        self.assertEqual(transport.uploads[0][0], (self.KEY + "\n").encode())
        script = transport.scripts[0]
        self.assertIn("/inheritance:r", script)
        self.assertIn(r"C:\ProgramData\ssh", script)
        self.assertIn("Restart-Service -Name sshd", transport.scripts[1])

    def test_skips_restart(self) -> None:
        transport = FakeTransport(
            lambda script: CommandResult(exitcode=0, stdout="", stderr="")
        )
        install_admin_key(transport, self.KEY, restart=False)
        self.assertEqual(len(transport.scripts), 1)

    def test_preserves_existing_keys(self) -> None:
        other = "ssh-rsa AAAAB3Nza other@host"
        transport = FakeTransport(
            lambda script: CommandResult(exitcode=0, stdout="", stderr="")
        )
        transport.existing_keys = (other + "\n").encode()
        install_admin_key(transport, self.KEY, restart=False)
        payload = transport.uploads[0][0].decode()
        self.assertIn(other, payload)
        self.assertIn(self.KEY, payload)

    def test_duplicate_key_is_not_added_twice(self) -> None:
        transport = FakeTransport(
            lambda script: CommandResult(exitcode=0, stdout="", stderr="")
        )
        transport.existing_keys = (self.KEY + "\n").encode()
        install_admin_key(transport, self.KEY, restart=False)
        payload = transport.uploads[0][0].decode()
        self.assertEqual(payload.count(self.KEY), 1)

    def test_rejects_invalid_key(self) -> None:
        transport = FakeTransport(lambda script: ok({}))
        with self.assertRaises(VemuError):
            install_admin_key(transport, "not-a-key")
        with self.assertRaises(VemuError):
            install_admin_key(transport, self.KEY + "\n" + self.KEY)
        self.assertEqual(transport.uploads, [])

    def test_acl_failure(self) -> None:
        transport = FakeTransport(
            lambda script: CommandResult(exitcode=1, stdout="", stderr="acl")
        )
        with self.assertRaises(VemuError):
            install_admin_key(transport, self.KEY)


class CertificateTests(unittest.TestCase):
    def test_certificate_fingerprint(self) -> None:
        from vemu.seamless.rdp import rdp_certificate_sha256

        hex_value = "A" * 64
        transport = FakeTransport(
            lambda script: CommandResult(
                exitcode=0, stdout=hex_value, stderr=""
            )
        )
        self.assertEqual(rdp_certificate_sha256(transport), hex_value)

    def test_certificate_invalid_value(self) -> None:
        from vemu.seamless.rdp import rdp_certificate_sha256

        transport = FakeTransport(lambda script: ok("not-a-fingerprint"))
        self.assertIsNone(rdp_certificate_sha256(transport))

    def test_key_only_ssh_script(self) -> None:
        from vemu.seamless.rdp import configure_ssh_key_only

        transport = FakeTransport(
            lambda script: ok(
                "pubkeyauthentication yes\n"
                "passwordauthentication no\n"
                "kbdinteractiveauthentication no"
            )
        )
        result = configure_ssh_key_only(transport)
        script = transport.scripts[0]
        self.assertIn("PasswordAuthentication no", script)
        self.assertIn("KbdInteractiveAuthentication no", script)
        self.assertIn("Restart-Service -Name sshd", script)
        self.assertFalse(result["password_authentication"])
        self.assertTrue(result["pubkey_authentication"])


class StandardUserTests(unittest.TestCase):
    def test_inspect_user_and_rdp_membership(self) -> None:
        from vemu.seamless.rdp import inspect_windows_user

        payload = {
            "user": "StdUser",
            "admin": False,
            "remote_desktop": True,
            "added_to_remote_desktop": True,
            "profile": r"C:\Users\StdUser",
        }
        transport = FakeTransport(lambda script: ok(payload))
        result = inspect_windows_user(transport, "StdUser")
        self.assertFalse(result["admin"])
        self.assertTrue(result["remote_desktop"])
        self.assertEqual(result["profile"], r"C:\Users\StdUser")
        self.assertIn("S-1-5-32-555", transport.scripts[0])

    def test_install_standard_user_key(self) -> None:
        from vemu.seamless.rdp import install_user_key

        transport = FakeTransport(lambda script: ok({"FullName": "x"}))
        result = install_user_key(
            transport,
            "StdUser",
            "ssh-ed25519 AAAAC3Nza user@host",
            restart=False,
        )
        self.assertTrue(result["installed"])
        self.assertIn("vemu-user-key.pub", transport.uploads[0][1])
        script = transport.scripts[0]
        self.assertIn(".ssh", script)
        self.assertIn("authorized_keys", script)
        self.assertIn("Win32_UserProfile", script)
        self.assertIn("/setowner", script)

    def test_install_key_requires_a_profile(self) -> None:
        from vemu.seamless.rdp import install_user_key

        transport = FakeTransport(
            lambda script: CommandResult(
                exitcode=1,
                stdout="",
                stderr="Log into Windows as this user once",
            )
        )
        with self.assertRaises(VemuError) as caught:
            install_user_key(
                transport,
                "StdUser",
                "ssh-ed25519 AAAAC3Nza user@host",
                restart=False,
            )
        self.assertIn("Log into Windows", str(caught.exception))
