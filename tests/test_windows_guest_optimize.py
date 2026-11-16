from __future__ import annotations

import fnmatch
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILES = ROOT / "contrib" / "windows" / "profiles.json"
SCRIPT = ROOT / "contrib" / "windows" / "vemu-guest-optimize.ps1"

PROTECTED = {
    "QEMU-GA",
    "vgpusrv",
    "VioGpuSrv",
    "VioGpuDod",
    "VioGpu3D",
    "BalloonService",
    "WinFsp",
    "viofs",
    "VirtioFsSvc",
    "vioserial",
    "sshd",
}
SECURITY = {
    "WinDefend",
    "mpssvc",
    "BFE",
    "SecurityHealthService",
    "wuauserv",
    "UsoSvc",
    "WaaSMedicSvc",
    "BDESVC",
}
POWER_GUID = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"


class WindowsGuestOptimizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.data = json.loads(PROFILES.read_text(encoding="utf-8"))
        cls.script = SCRIPT.read_text(encoding="utf-8")

    def all_services(self) -> list[str]:
        services = list(self.data["common"]["services"])
        for profile in self.data["profiles"].values():
            services.extend(profile.get("services", []))
        return services

    def all_tasks(self) -> list[dict[str, str]]:
        tasks = list(self.data["common"]["tasks"])
        for profile in self.data["profiles"].values():
            tasks.extend(profile.get("tasks", []))
        return tasks

    def test_schema_and_profiles(self) -> None:
        self.assertEqual(self.data["schemaVersion"], 1)
        self.assertEqual(self.data["license"], "GPL-3.0-or-later")
        self.assertEqual(
            set(self.data["profiles"]), {"Safe", "Balanced", "3D"}
        )
        self.assertGreaterEqual(len(self.data["common"]["services"]), 8)
        self.assertGreaterEqual(len(self.data["common"]["tasks"]), 40)
        self.assertGreaterEqual(len(self.data["common"]["hklm"]), 70)
        self.assertGreaterEqual(len(self.data["common"]["hkcu"]), 35)


    def test_no_duplicate_entries(self) -> None:
        def duplicates(items, key):
            seen = set()
            dupes = []
            for item in items:
                value = key(item)
                if value in seen:
                    dupes.append(value)
                seen.add(value)
            return dupes

        self.assertEqual(duplicates(self.data["common"]["services"], lambda x: x), [])
        self.assertEqual(
            duplicates(self.all_tasks(), lambda t: t["path"] + t["name"]), []
        )
        self.assertEqual(
            duplicates(self.data["common"]["hklm"], lambda r: r["key"] + r["name"]),
            [],
        )
        self.assertEqual(
            duplicates(self.data["common"]["hkcu"], lambda r: r["key"] + r["name"]),
            [],
        )
        self.assertEqual(
            duplicates(self.data["profiles"]["3D"]["hkcu"], lambda r: r["key"] + r["name"]),
            [],
        )

    def test_protected_features_are_declared(self) -> None:
        declared = set(self.data["protected"]["services"])
        patterns = self.data["protected"]["patterns"]
        self.assertTrue(PROTECTED <= declared)
        for expected in ("VioGpu*", "USB*", "Spice*", "Virtio*", "WinFsp*"):
            self.assertIn(expected, patterns)

    def test_services_never_touch_protected_or_security(self) -> None:
        protected = set(self.data["protected"]["services"])
        patterns = self.data["protected"]["patterns"]
        for name in self.all_services():
            self.assertNotIn(name, protected, name)
            for pattern in patterns:
                self.assertFalse(
                    fnmatch.fnmatch(name.lower(), pattern.lower()),
                    f"{name} matches protected pattern {pattern}",
                )
            self.assertNotIn(name, SECURITY, name)

    def test_tasks_are_safe(self) -> None:
        for task in self.all_tasks():
            path = task["path"]
            self.assertTrue(path.endswith("\\"), task)
            self.assertNotIn("WindowsUpdate", path, task)
            self.assertNotIn("UpdateOrchestrator", path, task)
            self.assertNotIn("InstallService", path, task)
            self.assertNotIn("BfeonServiceStartTypeChange", task["name"])
            self.assertNotIn("SystemRestore", path, task)

    def test_registry_entries_are_well_formed(self) -> None:
        allowed = {"REG_DWORD", "REG_SZ", "REG_BINARY", "REG_EXPAND_SZ"}
        for entry in self.data["common"]["hklm"]:
            self.assertTrue(entry["key"].startswith("HKLM\\"), entry)
            self.assertIn(entry["type"], allowed, entry)
            self.assertNotEqual(entry["data"], "", entry)
        for entry in self.data["common"]["hkcu"] + self.data["profiles"]["3D"]["hkcu"]:
            self.assertFalse(entry["key"].startswith("HKLM"), entry)
            self.assertIn(entry["type"], allowed, entry)
            self.assertNotEqual(entry["data"], "", entry)

    def test_power_policy_is_high_performance_and_no_sleep(self) -> None:
        power = self.data["common"]["power"]
        self.assertEqual(power["activeScheme"], POWER_GUID)
        changes = {item[0]: item[1] for item in power["change"]}
        for key in (
            "standby-timeout-ac",
            "standby-timeout-dc",
            "hibernate-timeout-ac",
            "hibernate-timeout-dc",
            "monitor-timeout-ac",
            "monitor-timeout-dc",
            "disk-timeout-ac",
            "disk-timeout-dc",
        ):
            self.assertEqual(changes.get(key), "0", key)
        self.assertTrue(power["hibernateOff"])
        self.assertTrue(power["hiberbootOff"])
        self.assertTrue(power["usbSelectiveSuspendOff"])
        self.assertTrue(power["pcieAspmOff"])

    def test_script_contract(self) -> None:
        for text in (
            "ValidateSet('Safe', 'Balanced', '3D')",
            "DryRun",
            "UserHive",
            "SkipPower",
            "Test-ProtectedService",
            "QEMU-GA",
            "WinFsp",
            "sshd",
            "powercfg.exe",
        ):
            self.assertIn(text, self.script)
        self.assertNotIn("ProUserTest", self.script)
        self.assertNotIn("DefaultPassword", self.script)
        commands = self.data["common"]["commands"]
        self.assertTrue(
            any(item["file"] == "fsutil.exe" for item in commands), commands
        )

    def test_dry_run_plans_all_profiles(self) -> None:
        pwsh = shutil.which("pwsh")
        if not pwsh:
            self.skipTest("pwsh is not installed")
        for profile, services, tasks in (
            ("Safe", 11, 45),
            ("Balanced", 24, 54),
            ("3D", 11, 45),
        ):
            with tempfile.NamedTemporaryFile(
                prefix="vemu-guest-opt-", suffix=".log"
            ) as log:
                proc = subprocess.run(
                    [
                        pwsh,
                        "-NoProfile",
                        "-File",
                        str(SCRIPT),
                        "-Profile",
                        profile,
                        "-DryRun",
                        "-LogPath",
                        log.name,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(
                f"services={services} tasks={tasks}", proc.stdout, profile
            )
            self.assertIn("[dry-run]", proc.stdout, profile)


if __name__ == "__main__":
    unittest.main()
