from __future__ import annotations

import json
import unittest

from vemu.errors import VemuError
from vemu.guest.transport import CommandResult
from vemu.guest.windows import (
    WindowsInfo,
    query_services,
    query_windows_info,
    restart_service,
    set_registry_dword,
    set_service,
)


class FakeTransport:
    def __init__(self, responses: dict[str, CommandResult] | None = None) -> None:
        self.responses = responses or {}
        self.scripts: list[str] = []

    def run_powershell(self, script: str, *, timeout: int = 120) -> CommandResult:
        self.scripts.append(script)
        for needle, response in self.responses.items():
            if needle in script:
                return response
        return CommandResult(exitcode=0, stdout="{}", stderr="")

    def ping(self) -> bool:
        return True

    def run(self, *args, **kwargs) -> CommandResult:
        return CommandResult(exitcode=0, stdout="", stderr="")

    def upload(self, data: bytes, remote: str) -> None:
        pass

    def download(self, remote: str, *, max_bytes: int = 0) -> bytes:
        return b""


def ok(payload: dict) -> CommandResult:
    return CommandResult(
        exitcode=0, stdout=json.dumps(payload), stderr=""
    )


class WindowsInfoTests(unittest.TestCase):
    def test_parses_windows_info(self) -> None:
        transport = FakeTransport(
            {
                "Win32_ComputerSystem": ok(
                    {
                        "product_name": "Windows 11 Pro",
                        "display_version": "26H2",
                        "build": "26300",
                        "ubr": 9711,
                        "edition_id": "Professional",
                        "installation_type": "Client",
                        "hostname": "LAB",
                        "manufacturer": "CoreSystem",
                        "model": "vEMU",
                    }
                )
            }
        )
        info = query_windows_info(transport)
        self.assertEqual(info.build, "26300")
        self.assertEqual(info.ubr, 9711)
        self.assertTrue(info.is_windows_11)
        self.assertTrue(info.supports_seamless)

    def test_old_build_and_home_edition(self) -> None:
        info = WindowsInfo(
            product_name="Windows 10 Home",
            display_version="22H2",
            build="19045",
            ubr=1,
            edition_id="Core",
            installation_type="Client",
            hostname="LAB",
            manufacturer="",
            model="",
        )
        self.assertFalse(info.is_windows_11)
        self.assertFalse(info.supports_seamless)

    def test_invalid_json(self) -> None:
        transport = FakeTransport(
            {
                "Win32_ComputerSystem": CommandResult(
                    exitcode=0, stdout="not json", stderr=""
                )
            }
        )
        with self.assertRaises(VemuError):
            query_windows_info(transport)

    def test_query_failure(self) -> None:
        transport = FakeTransport(
            {
                "Win32_ComputerSystem": CommandResult(
                    exitcode=1, stdout="", stderr="denied"
                )
            }
        )
        with self.assertRaises(VemuError):
            query_windows_info(transport)


class ServiceTests(unittest.TestCase):
    def test_missing_service_is_reported(self) -> None:
        transport = FakeTransport(
            {
                "Get-Service": ok(
                    {
                        "QEMU-GA": {
                            "present": True,
                            "status": "Running",
                            "start_type": "Automatic",
                        }
                    }
                )
            }
        )
        services = query_services(transport, ["QEMU-GA", "sshd"])
        self.assertEqual(services["QEMU-GA"]["status"], "Running")
        self.assertNotIn("sshd", services)

    def test_name_quoting(self) -> None:
        transport = FakeTransport()
        query_services(transport, ["a'b"])
        self.assertIn("'a''b'", transport.scripts[-1])

    def test_set_registry_dword(self) -> None:
        transport = FakeTransport()
        set_registry_dword(transport, r"HKLM:\X'Y", "Value", 1)
        script = transport.scripts[-1]
        self.assertIn(r"HKLM:\X''Y", script)
        self.assertIn("Set-ItemProperty", script)
        self.assertIn("-Type DWord", script)
        self.assertIn(" -Value 1", script)

    def test_set_service(self) -> None:
        transport = FakeTransport()
        set_service(transport, "sshd", startup="Automatic", start=True)
        script = transport.scripts[-1]
        self.assertIn("Set-Service -Name 'sshd' -StartupType Automatic", script)
        self.assertIn("Start-Service -Name 'sshd'", script)

    def test_restart_service(self) -> None:
        transport = FakeTransport()
        restart_service(transport, "sshd")
        self.assertIn("Restart-Service -Name 'sshd' -Force", transport.scripts[-1])


class EnsureServiceTests(unittest.TestCase):
    def test_sets_automatic_and_starts(self) -> None:
        from vemu.guest.windows import ensure_service

        transport = FakeTransport(
            {
                "Set-Service": ok(
                    {
                        "name": "ssh-agent",
                        "present": True,
                        "status": "Running",
                        "start_type": "Automatic",
                        "error": "",
                    }
                )
            }
        )
        report = ensure_service(transport, "ssh-agent")
        self.assertTrue(report["present"])
        self.assertEqual(report["start_type"], "Automatic")
        self.assertIn("Set-Service", transport.scripts[-1])
        self.assertIn("Start-Service", transport.scripts[-1])

    def test_missing_service_is_reported(self) -> None:
        from vemu.guest.windows import ensure_service

        transport = FakeTransport(
            {
                "Set-Service": ok(
                    {
                        "name": "missing",
                        "present": False,
                        "status": "",
                        "start_type": "",
                        "error": "",
                    }
                )
            }
        )
        report = ensure_service(transport, "missing")
        self.assertFalse(report["present"])


class PendingRebootTests(unittest.TestCase):
    def test_yes(self) -> None:
        from vemu.guest.windows import pending_reboot

        transport = FakeTransport({"RebootRequired": close_ok("yes")})
        self.assertTrue(pending_reboot(transport))

    def test_no(self) -> None:
        from vemu.guest.windows import pending_reboot

        transport = FakeTransport({"RebootRequired": close_ok("no")})
        self.assertFalse(pending_reboot(transport))


def close_ok(text: str) -> CommandResult:
    return CommandResult(exitcode=0, stdout=text, stderr="")
