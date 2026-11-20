from __future__ import annotations

import json
import unittest

from vemu.errors import VemuError
from vemu.guest.optimize import (
    HIGH_PERFORMANCE,
    OptimizationReport,
    _check_state,
    _load_profile,
    apply_profile,
    assets_dir,
    collect_state,
    protected_services,
)
from vemu.guest.transport import CommandResult


def ok(payload: object | str = "") -> CommandResult:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return CommandResult(exitcode=0, stdout=text, stderr="")


SERVICES = {
    "QEMU-GA": {
        "present": True,
        "status": "Running",
        "start_type": "Automatic",
    },
    "sshd": {
        "present": True,
        "status": "Running",
        "start_type": "Automatic",
    },
}


class FakeTransport:
    def __init__(self, *, exitcode: int = 0, log: str = "done") -> None:
        self.exitcode = exitcode
        self.log = log
        self.scripts: list[str] = []
        self.runs: list[tuple[str, tuple[str, ...]]] = []
        self.uploads: list[tuple[bytes, str]] = []

    def run_powershell(self, script: str, *, timeout: int = 120) -> CommandResult:
        self.scripts.append(script)
        if "Get-Service -Name" in script:
            return ok(SERVICES)
        return ok()

    def run(self, path, args=(), *, timeout: int = 30) -> CommandResult:
        self.runs.append((path, tuple(args)))
        joined = path + " " + " ".join(args)
        if "getactivescheme" in joined:
            return ok(
                f"Power Scheme GUID: {HIGH_PERFORMANCE}  (High performance)\n"
            )
        if "reg.exe" in path:
            name = args[-1]
            if name == "HiberbootEnabled":
                return ok("    HiberbootEnabled    REG_DWORD    0x0\n")
            return ok("    HibernateEnabled    REG_DWORD    0x0\n")
        if "powercfg.exe" in path:
            return ok(
                "  Current AC Power Setting Index: 0x00000000\n"
                "  Current DC Power Setting Index: 0x00000000\n"
            )
        if "powershell.exe" in path:
            return CommandResult(
                exitcode=self.exitcode,
                stdout="log line\n",
                stderr="",
            )
        return ok()

    def upload(self, data: bytes, remote: str) -> None:
        self.uploads.append((data, remote))

    def download(self, remote: str, *, max_bytes: int = 0) -> bytes:
        return self.log.encode()

    def ping(self) -> bool:
        return True


class AssetTests(unittest.TestCase):
    def test_assets_are_the_shared_contrib_files(self) -> None:
        directory = assets_dir()
        self.assertEqual(directory.name, "windows")
        self.assertTrue((directory / "vemu-guest-optimize.ps1").is_file())
        self.assertTrue((directory / "profiles.json").is_file())

    def test_profiles_are_shared_not_copied(self) -> None:
        data = _load_profile("Safe")
        self.assertEqual(data["schemaVersion"], 1)
        self.assertIn("Safe", data["profiles"])

    def test_unknown_profile(self) -> None:
        with self.assertRaises(VemuError):
            _load_profile("Turbo")

    def test_protected_services_include_vemu_plumbing(self) -> None:
        names = protected_services()
        for expected in ("QEMU-GA", "sshd", "TermService", "WinFsp", "viofs"):
            self.assertIn(expected, names)


class CheckStateTests(unittest.TestCase):
    def good_state(self) -> dict:
        return {
            "active_scheme": {
                "guid": HIGH_PERFORMANCE,
                "name": "High performance",
            },
            "hiberboot": 0,
            "hibernate_enabled": 0,
            "standby_timeout": {"ac": 0, "dc": 0},
            "usb_selective_suspend": {"ac": 0, "dc": 0},
            "pcie_aspm": {"ac": 0, "dc": 0},
            "services": dict(SERVICES),
        }

    def test_good_state(self) -> None:
        self.assertEqual(_check_state(self.good_state(), baseline=SERVICES), [])

    def test_wrong_power_plan(self) -> None:
        state = self.good_state()
        state["active_scheme"]["guid"] = "00000000-0000-0000-0000-000000000000"
        problems = _check_state(state, baseline=None)
        self.assertTrue(any("High performance" in p for p in problems))

    def test_fast_startup_and_hibernation(self) -> None:
        state = self.good_state()
        state["hiberboot"] = 1
        state["hibernate_enabled"] = 1
        problems = _check_state(state, baseline=None)
        self.assertTrue(any("Fast Startup" in p for p in problems))
        self.assertTrue(any("Hibernation" in p for p in problems))

    def test_timeouts_and_suspend(self) -> None:
        state = self.good_state()
        state["standby_timeout"] = {"ac": 300, "dc": 0}
        state["usb_selective_suspend"] = {"ac": 0, "dc": 1}
        state["pcie_aspm"] = {"ac": 1, "dc": 0}
        problems = _check_state(state, baseline=None)
        self.assertTrue(any("Standby" in p for p in problems))
        self.assertTrue(any("USB selective suspend" in p for p in problems))
        self.assertTrue(any("PCIe ASPM" in p for p in problems))

    def test_protected_service_regression(self) -> None:
        state = self.good_state()
        state["services"]["QEMU-GA"] = {
            "present": True,
            "status": "Stopped",
            "start_type": "Manual",
        }
        problems = _check_state(state, baseline=SERVICES)
        self.assertTrue(any("QEMU-GA" in p for p in problems))

    def test_transient_service_is_not_a_regression(self) -> None:
        baseline = dict(SERVICES)
        baseline["StateRepository"] = {
            "present": True,
            "status": "Running",
            "start_type": "Automatic",
        }
        state = self.good_state()
        state["services"]["StateRepository"] = {
            "present": True,
            "status": "Stopped",
            "start_type": "Automatic",
        }
        self.assertEqual(_check_state(state, baseline=baseline), [])

    def test_missing_optional_values_are_ignored(self) -> None:
        state = self.good_state()
        state["usb_selective_suspend"] = None
        state["hiberboot"] = None
        self.assertEqual(_check_state(state, baseline=None), [])


class CollectStateTests(unittest.TestCase):
    def test_collects_power_registry_and_services(self) -> None:
        state = collect_state(FakeTransport(), services=["QEMU-GA"])
        self.assertEqual(state["active_scheme"]["guid"], HIGH_PERFORMANCE)
        self.assertEqual(state["hiberboot"], 0)
        self.assertEqual(state["hibernate_enabled"], 0)
        self.assertEqual(state["standby_timeout"], {"ac": 0, "dc": 0})
        self.assertEqual(state["services"]["QEMU-GA"]["status"], "Running")


class ApplyProfileTests(unittest.TestCase):
    def test_uploads_shared_files_and_runs_profile(self) -> None:
        transport = FakeTransport()
        report = apply_profile(transport, "Safe", verify=False)
        self.assertTrue(report.ok)
        remotes = [remote for _, remote in transport.uploads]
        self.assertIn(r"C:\ProgramData\vEMU\guest\vemu-guest-optimize.ps1", remotes)
        self.assertIn(r"C:\ProgramData\vEMU\guest\profiles.json", remotes)
        path, args = transport.runs[-1]
        self.assertIn("powershell.exe", path)
        self.assertIn("-File", args)
        self.assertIn("-Profile", args)
        self.assertEqual(args[args.index("-Profile") + 1], "Safe")
        self.assertIn("-LogPath", args)
        self.assertNotIn("-DryRun", args)

    def test_dry_run_skips_verification(self) -> None:
        transport = FakeTransport()
        report = apply_profile(transport, "Safe", dry_run=True)
        path, args = transport.runs[-1]
        self.assertIn("-DryRun", args)
        self.assertEqual(report.state, {})
        self.assertEqual(report.problems, ())

    def test_optional_flags(self) -> None:
        transport = FakeTransport()
        apply_profile(
            transport,
            "Balanced",
            user_hive=r"C:\Users\Lab\NTUSER.DAT",
            skip_power=True,
            skip_user_settings=True,
            verify=False,
        )
        _, args = transport.runs[-1]
        self.assertIn("-SkipPower", args)
        self.assertIn("-SkipUserSettings", args)
        self.assertEqual(
            args[args.index("-UserHive") + 1], r"C:\Users\Lab\NTUSER.DAT"
        )

    def test_verifies_after_run(self) -> None:
        transport = FakeTransport()
        report = apply_profile(transport, "Safe")
        self.assertTrue(report.ok)
        self.assertEqual(report.state["active_scheme"]["guid"], HIGH_PERFORMANCE)

    def test_failure_raises_with_log_tail(self) -> None:
        transport = FakeTransport(exitcode=1, log="line\nPROTECTED FEATURE LOST\n")
        with self.assertRaises(VemuError) as caught:
            apply_profile(transport, "Safe")
        self.assertIn("PROTECTED FEATURE LOST", str(caught.exception))
        self.assertIn("exit 1", str(caught.exception))

    def test_failure_can_return_report(self) -> None:
        transport = FakeTransport(exitcode=1, log="failed")
        report = apply_profile(transport, "Safe", strict=False)
        self.assertFalse(report.ok)
        self.assertEqual(report.exitcode, 1)

    def test_verification_failure_raises(self) -> None:
        transport = FakeTransport()
        bad = {
            "active_scheme": {"guid": HIGH_PERFORMANCE, "name": "High performance"},
            "hiberboot": 1,
            "hibernate_enabled": 0,
            "standby_timeout": {"ac": 0, "dc": 0},
            "usb_selective_suspend": {"ac": 0, "dc": 0},
            "pcie_aspm": {"ac": 0, "dc": 0},
            "services": {},
        }
        original = collect_state

        import vemu.guest.optimize as optimize

        optimize.collect_state = lambda *args, **kwargs: bad
        try:
            with self.assertRaises(VemuError) as caught:
                apply_profile(transport, "Safe")
        finally:
            optimize.collect_state = original
        self.assertIn("Fast Startup", str(caught.exception))

    def test_report_property(self) -> None:
        report = OptimizationReport(
            profile="Safe", exitcode=0, log="", state={}, problems=()
        )
        self.assertTrue(report.ok)
        report = OptimizationReport(
            profile="Safe", exitcode=0, log="", state={}, problems=("x",)
        )
        self.assertFalse(report.ok)
