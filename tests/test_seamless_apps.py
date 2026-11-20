from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu.errors import VemuError
from vemu.guest.transport import CommandResult
from vemu.seamless import launch
from vemu.seamless.apps import (
    AppEntry,
    discover_start_menu_apps,
    parse_entries,
    store_aumid,
    with_compat_arguments,
)
from vemu.seamless.overlay import apply_overlay


class FakeTransport:
    def __init__(self, payload: object | str = "[]", *, ok: bool = True) -> None:
        self.payload = payload
        self._ok = ok
        self.scripts: list[str] = []
        self.uploads: list[tuple[bytes, str]] = []

    def run_powershell(self, script: str, *, timeout: int = 120) -> CommandResult:
        self.scripts.append(script)
        text = (
            self.payload
            if isinstance(self.payload, str)
            else json.dumps(self.payload)
        )
        return CommandResult(
            exitcode=0 if self._ok else 1, stdout=text, stderr=""
        )

    def run(self, *args, **kwargs) -> CommandResult:
        return CommandResult(exitcode=0, stdout="", stderr="")

    def upload(self, data: bytes, remote: str) -> None:
        self.uploads.append((data, remote))

    def download(self, remote: str, *, max_bytes: int = 0) -> bytes:
        return b""

    def ping(self) -> bool:
        return True


class AppDiscoveryTests(unittest.TestCase):
    def test_filters_uwp_and_non_exe(self) -> None:
        entries = parse_entries(
            [
                {
                    "name": "Calculator",
                    "path": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App",
                },
                {"name": "Shell", "path": "shell:AppsFolder\\x"},
                {"name": "Bad", "path": "C:\\bad.txt"},
                {
                    "name": "Notepad++",
                    "path": "C:\\Program Files\\Notepad++\\notepad++.exe",
                },
                {
                    "name": "Notepad++",
                    "path": "C:\\Program Files\\Notepad++\\notepad++.exe",
                },
            ]
        )
        self.assertEqual([item.name for item in entries], ["Notepad++"])

    def test_keeps_arguments_and_shortcut(self) -> None:
        entries = parse_entries(
            {
                "name": "OnlyOffice Document",
                "path": "C:\\Program Files\\ONLYOFFICE\\DesktopEditors.exe",
                "arguments": "--new:word",
                "shortcut": "C:\\Start\\Document.lnk",
            }
        )
        self.assertEqual(entries[0].arguments, "--new:word")
        self.assertEqual(entries[0].shortcut, "C:\\Start\\Document.lnk")

    def test_process_name(self) -> None:
        entry = AppEntry("Notepad", "C:\\Windows\\System32\\notepad.exe")
        self.assertEqual(entry.process_name, "notepad")

    def test_chromium_flags(self) -> None:
        self.assertEqual(
            with_compat_arguments("C:\\Program Files\\Microsoft\\Edge\\msedge.exe"),
            "--disable-gpu --disable-gpu-compositing",
        )
        self.assertEqual(
            with_compat_arguments(
                "C:\\Tools\\chrome.exe", "--profile-directory=Default"
            ),
            "--profile-directory=Default --disable-gpu "
            "--disable-gpu-compositing",
        )

    def test_chromium_flags_not_duplicated(self) -> None:
        value = with_compat_arguments(
            "C:\\Tools\\chrome.exe", "--disable-gpu --disable-gpu-compositing"
        )
        self.assertEqual(value.split().count("--disable-gpu"), 1)

    def test_non_chromium_untouched(self) -> None:
        self.assertEqual(
            with_compat_arguments("C:\\Windows\\notepad.exe", "--new"), "--new"
        )

    def test_store_apps_hidden_by_default(self) -> None:
        payload = [
            {
                "name": "Calculator",
                "path": "C:\\Windows\\explorer.exe",
                "arguments": (
                    "shell:AppsFolder\\"
                    "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
                ),
                "source": "store",
            }
        ]
        self.assertEqual(parse_entries(payload), [])
        entries = parse_entries(payload, include_store=True)
        self.assertEqual([item.name for item in entries], ["Calculator"])
        self.assertEqual(entries[0].path, "C:\\Windows\\explorer.exe")
        self.assertEqual(entries[0].source, "store")

    def test_store_shell_apps_are_filtered(self) -> None:
        payload = [
            {
                "name": "Get Started",
                "path": "C:\\Windows\\explorer.exe",
                "arguments": (
                    "shell:AppsFolder\\"
                    "MicrosoftWindows.Client.CBS_cw5n1h2txyewy!WebExperienceHost"
                ),
                "source": "store",
            },
            {
                "name": "Calculator",
                "path": "C:\\Windows\\explorer.exe",
                "arguments": (
                    "shell:AppsFolder\\"
                    "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
                ),
                "source": "store",
            },
        ]
        entries = parse_entries(payload, include_store=True)
        self.assertEqual([item.name for item in entries], ["Calculator"])

    def test_store_aumid(self) -> None:
        value = store_aumid(
            "shell:AppsFolder\\Microsoft.Paint_8wekyb3d8bbwe!App"
        )
        self.assertEqual(value, "Microsoft.Paint_8wekyb3d8bbwe!App")
        self.assertEqual(store_aumid("--new"), "")

    def test_system_tools_hidden_by_default(self) -> None:
        payload = [
            {
                "name": "Command Prompt",
                "path": "C:\\Windows\\System32\\cmd.exe",
                "source": "start-menu",
            },
            {
                "name": "Notepad++",
                "path": "C:\\Program Files\\Notepad++\\notepad++.exe",
                "source": "start-menu",
            },
        ]
        self.assertEqual(
            [item.name for item in parse_entries(payload)],
            ["Notepad++"],
        )
        self.assertEqual(
            [item.name for item in parse_entries(payload, include_system=True)],
            ["Command Prompt", "Notepad++"],
        )

    def test_program_files_fallback_skips_shortcut_path(self) -> None:
        payload = [
            {
                "name": "Vendor App",
                "path": "C:\\Program Files\\Vendor\\app.exe",
                "source": "start-menu",
            },
            {
                "name": "app",
                "path": "C:\\Program Files\\Vendor\\app.exe",
                "source": "program-files",
            },
        ]
        entries = parse_entries(payload)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].name, "Vendor App")

    def test_program_files_fallback_kept_without_shortcut(self) -> None:
        entries = parse_entries(
            [
                {
                    "name": "Vendor App",
                    "path": "C:\\Program Files\\Vendor\\app.exe",
                    "source": "program-files",
                }
            ]
        )
        self.assertEqual(entries[0].source, "program-files")

    def test_discover_store_opt_in(self) -> None:
        transport = FakeTransport(
            [
                {
                    "name": "Calculator",
                    "path": "C:\\Windows\\explorer.exe",
                    "arguments": (
                        "shell:AppsFolder\\"
                        "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
                    ),
                    "shortcut": "",
                    "source": "store",
                }
            ]
        )
        apps = discover_start_menu_apps(transport, include_store=True)
        self.assertEqual([item.name for item in apps], ["Calculator"])
        script = transport.uploads[0][0].decode("utf-8")
        self.assertIn("$includeStore = $true", script)
        self.assertIn("Get-StartApps", script)

        transport = FakeTransport([])
        discover_start_menu_apps(transport)
        script = transport.uploads[0][0].decode("utf-8")
        self.assertIn("$includeStore = $false", script)

    def test_discover_uses_start_menu_links(self) -> None:
        transport = FakeTransport(
            [
                {
                    "name": "Notepad++",
                    "path": "C:\\Program Files\\Notepad++\\notepad++.exe",
                    "arguments": "",
                    "shortcut": "C:\\Start\\Notepad++.lnk",
                    "source": "start-menu",
                }
            ]
        )
        apps = discover_start_menu_apps(transport)
        self.assertEqual(apps[0].name, "Notepad++")
        self.assertTrue(transport.uploads)
        script = transport.uploads[0][0].decode("utf-8")
        self.assertIn("WScript.Shell", script)
        self.assertIn("Start Menu", script)
        self.assertIn("$env:ProgramFiles", script)
        self.assertTrue(transport.scripts[0].startswith("& "))

    def test_discover_failure(self) -> None:
        transport = FakeTransport("nope", ok=False)
        with self.assertRaises(VemuError):
            discover_start_menu_apps(transport)


class SessionLaunchTests(unittest.TestCase):
    def test_scheduled_task_launch(self) -> None:
        transport = FakeTransport({"task": "vemu-launch-x", "state": "Ready"})
        result = launch.launch_in_session(
            transport,
            "ProUser",
            "Word",
            r"C:\Program Files\Microsoft Office\root\Office16\WINWORD.EXE",
            "--new",
        )
        script = transport.scripts[0]
        self.assertIn("Register-ScheduledTask", script)
        self.assertIn("Start-ScheduledTask", script)
        self.assertIn("-UserId 'ProUser'", script)
        self.assertIn("-LogonType Interactive", script)
        self.assertIn("-Argument '--new'", script)
        self.assertTrue(result["task"].startswith("vemu-launch-"))

    def test_escapes_single_quotes(self) -> None:
        transport = FakeTransport({"task": "x", "state": "Ready"})
        launch.launch_in_session(
            transport, "O'Brien", "O'Brien", r"C:\O'Brien\app.exe"
        )
        script = transport.scripts[0]
        self.assertIn("O''Brien", script)

    def test_failure(self) -> None:
        transport = FakeTransport("", ok=False)
        with self.assertRaises(VemuError):
            launch.launch_in_session(
                transport, "ProUser", "Word", r"C:\Word.exe"
            )

    def test_stop_seed(self) -> None:
        transport = FakeTransport("1")
        self.assertEqual(launch.stop_seed(transport), 1)
        self.assertIn("vemu-rail-seed", transport.scripts[0])
        self.assertIn("wscript.exe", transport.scripts[0])


class OverlayTests(unittest.TestCase):
    def test_sets_edge_and_chrome_policies(self) -> None:
        transport = FakeTransport([])
        result = apply_overlay(transport)
        script = transport.scripts[0]
        self.assertIn("Policies\\Microsoft\\Edge", script)
        self.assertIn("Policies\\Google\\Chrome", script)
        self.assertIn("HardwareAccelerationModeEnabled", script)
        self.assertTrue(result["hardware_acceleration_disabled"])

    def test_failure(self) -> None:
        transport = FakeTransport("", ok=False)
        with self.assertRaises(VemuError):
            apply_overlay(transport)


class FakeStdin:
    def __init__(self) -> None:
        self.value = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.value += data

    def close(self) -> None:
        self.closed = True


class FakeProcess:
    def __init__(self, log, *, output: bytes = b"", exit_at: int | None = None):
        self.stdin = FakeStdin()
        self.stdout = log
        self.returncode: int | None = None
        self.polls = 0
        self.exit_at = exit_at
        self.terminated = False
        if output:
            log.write(output)
            log.flush()

    def poll(self):
        self.polls += 1
        if self.exit_at is not None and self.polls >= self.exit_at:
            self.returncode = 1
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        status = launch.FreeRdpStatus(
            "/usr/bin/xfreerdp",
            (3, 33, 1),
            "ok",
            "FreeRDP 3.33.1",
        )
        patcher = mock.patch.object(
            launch, "ensure_freerdp_compatible", return_value=status
        )
        self.freerdp_status = patcher.start()
        self.addCleanup(patcher.stop)

    def test_freerdp_binary_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "xfreerdp"
            path.write_text("#!/bin/sh\n", encoding="utf-8")
            path.chmod(0o755)
            with mock.patch.dict(
                os.environ, {launch.FREERDP_ENV: str(path)}
            ):
                self.assertEqual(launch.freerdp_binary(), str(path))

    def test_freerdp_binary_override_must_be_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "xfreerdp"
            path.write_text("", encoding="utf-8")
            path.chmod(0o644)
            with mock.patch.dict(
                os.environ, {launch.FREERDP_ENV: str(path)}
            ), self.assertRaises(VemuError):
                launch.freerdp_binary()

    def test_freerdp_binary_falls_back_to_path(self) -> None:
        with mock.patch.dict(
            os.environ, {launch.FREERDP_ENV: ""}
        ), mock.patch.object(
            launch.shutil, "which", return_value="/usr/bin/xfreerdp"
        ):
            self.assertEqual(launch.freerdp_binary(), "/usr/bin/xfreerdp")

    def test_freerdp_binary_accepts_xfreerdp3(self) -> None:
        def which(name: str) -> str | None:
            return "/usr/bin/xfreerdp3" if name == "xfreerdp3" else None

        with mock.patch.dict(
            os.environ, {launch.FREERDP_ENV: ""}
        ), mock.patch.object(launch.shutil, "which", side_effect=which):
            self.assertEqual(launch.freerdp_binary(), "/usr/bin/xfreerdp3")

    def test_freerdp_version_parses_output(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            0,
            "This is FreeRDP version 3.26.0 (3.26.0)\n",
            "",
        )
        with mock.patch.object(
            launch.subprocess, "run", return_value=completed
        ):
            self.assertEqual(
                launch.freerdp_version("/usr/bin/xfreerdp"), (3, 26, 0)
            )

    def test_freerdp_version_sets_home_without_daemon_home(self) -> None:
        completed = subprocess.CompletedProcess(
            [],
            0,
            "This is FreeRDP version 3.33.1 (n/a)\n",
            "",
        )
        with mock.patch.dict(
            launch.os.environ, {"PATH": "/usr/bin"}, clear=True
        ), mock.patch.object(
            launch.subprocess, "run", return_value=completed
        ) as run:
            self.assertEqual(
                launch.freerdp_version("/usr/bin/xfreerdp"), (3, 33, 1)
            )
        self.assertTrue(run.call_args.kwargs["env"].get("HOME"))

    def test_freerdp_status_blocks_old_client(self) -> None:
        with mock.patch.object(
            launch, "freerdp_version", return_value=(3, 15, 0)
        ):
            status = launch.freerdp_status("/usr/bin/xfreerdp")
        self.assertEqual(status.level, "unsupported")

    def test_freerdp_status_warns_before_icon_fix(self) -> None:
        with mock.patch.object(
            launch, "freerdp_version", return_value=(3, 26, 0)
        ):
            status = launch.freerdp_status("/usr/bin/xfreerdp")
        self.assertEqual(status.level, "warn")

    def test_freerdp_status_accepts_current_release(self) -> None:
        with mock.patch.object(
            launch, "freerdp_version", return_value=(3, 32, 1)
        ):
            status = launch.freerdp_status("/usr/bin/xfreerdp")
        self.assertEqual(status.level, "ok")

    def test_build_arguments(self) -> None:
        lines = launch.build_arguments(
            "192.0.2.10",
            "ProUser",
            "secret",
            r"C:\Windows\notepad.exe",
            "--new",
            size="1280x800",
        )
        self.assertIn("/v:192.0.2.10", lines)
        self.assertIn("/u:ProUser", lines)
        self.assertIn("/p:secret", lines)
        self.assertIn(
            r"/app:program:C:\Windows\notepad.exe,cmd:--new", lines
        )

    def test_reconnect_arguments(self) -> None:
        lines = launch.build_arguments(
            "h", "u", "secret", r"C:\x.exe", reconnect=True
        )
        self.assertIn("+auto-reconnect", lines)
        self.assertIn("/auto-reconnect-max-retries:20", lines)

    def test_input_grabs_are_disabled_for_rail(self) -> None:
        lines = launch.build_arguments("h", "u", "secret", r"C:\x.exe")
        self.assertIn("-grab-keyboard", lines)
        self.assertIn("-grab-mouse", lines)
        self.assertIn("/mouse:grab:off", lines)
        grabbed = launch.build_arguments(
            "h", "u", "secret", r"C:\x.exe", grab_input=True
        )
        self.assertNotIn("-grab-mouse", grabbed)

    def test_audio_is_enabled_by_default(self) -> None:
        lines = launch.build_arguments("h", "u", "secret", r"C:\x.exe")
        sound = [line for line in lines if line.startswith("/sound")]
        self.assertEqual(len(sound), 1)
        self.assertIn("latency:200", sound[0])
        self.assertIn("quality:dynamic", sound[0])

    def test_multimon_is_opt_in(self) -> None:
        plain = launch.build_arguments("h", "u", "secret", r"C:\x.exe")
        self.assertNotIn("+multimon", plain)
        multi = launch.build_arguments(
            "h", "u", "secret", r"C:\x.exe", multimon=True
        )
        self.assertIn("+multimon", multi)

    def test_password_newline_rejected(self) -> None:
        with self.assertRaises(VemuError):
            launch.build_arguments(
                "h", "u", "bad\npassword", r"C:\x.exe"
            )

    def test_success(self) -> None:
        process = None

        def fake_popen(*args, **kwargs):
            nonlocal process
            process = FakeProcess(kwargs["stdout"])
            return process

        with mock.patch.object(
            launch.shutil, "which", return_value="/usr/bin/xfreerdp"
        ), mock.patch.object(
            launch.subprocess, "Popen", side_effect=fake_popen
        ):
            result = launch.test_rail_app(
                "h", "u", "secret", r"C:\x.exe", settle=0, timeout=1
            )
        self.assertTrue(result.ok)
        self.assertTrue(result.connected)
        self.assertTrue(process.stdin.closed)
        self.assertIn(b"/p:secret", process.stdin.value)

    def test_allowlist_failure(self) -> None:
        def fake_popen(*args, **kwargs):
            return FakeProcess(
                kwargs["stdout"],
                output=(
                    b"RAIL exec error: "
                    b"execResult=RAIL_EXEC_E_NOT_IN_ALLOWLIST\n"
                ),
            )

        with mock.patch.object(
            launch.shutil, "which", return_value="/usr/bin/xfreerdp"
        ), mock.patch.object(
            launch.subprocess, "Popen", side_effect=fake_popen
        ):
            result = launch.test_rail_app(
                "h", "u", "secret", r"C:\x.exe", settle=0, timeout=1
            )
        self.assertFalse(result.ok)
        self.assertFalse(result.connected)
        self.assertIn("allowlist", result.reason)
        self.assertNotIn("secret", result.log)

    def test_exit_before_settle(self) -> None:
        def fake_popen(*args, **kwargs):
            return FakeProcess(kwargs["stdout"], exit_at=1)

        with mock.patch.object(
            launch.shutil, "which", return_value="/usr/bin/xfreerdp"
        ), mock.patch.object(
            launch.subprocess, "Popen", side_effect=fake_popen
        ):
            result = launch.test_rail_app(
                "h", "u", "secret", r"C:\x.exe", settle=5, timeout=5
            )
        self.assertFalse(result.ok)
        self.assertEqual(result.exitcode, 1)

    def test_timeout(self) -> None:
        def fake_popen(*args, **kwargs):
            return FakeProcess(kwargs["stdout"])

        with mock.patch.object(
            launch.shutil, "which", return_value="/usr/bin/xfreerdp"
        ), mock.patch.object(
            launch.subprocess, "Popen", side_effect=fake_popen
        ):
            result = launch.test_rail_app(
                "h", "u", "secret", r"C:\x.exe",
                settle=10, timeout=0.3,
            )
        self.assertFalse(result.ok)
        self.assertIn("timed out", result.reason)


class FreeRdpCompatibilityTests(unittest.TestCase):
    def test_ensure_blocks_old_client(self) -> None:
        with mock.patch.object(
            launch, "freerdp_version", return_value=(3, 15, 0)
        ):
            with self.assertRaises(VemuError):
                launch.ensure_freerdp_compatible("/usr/bin/xfreerdp")


class WatchTests(unittest.TestCase):
    def test_counts_rail_windows(self) -> None:
        from vemu.seamless import watch

        root = subprocess.CompletedProcess(
            [], 0, "_NET_CLIENT_LIST(WINDOW): # 0x100, 0x200, 0x300", ""
        )
        classes = {
            "0x100": 'WM_CLASS(STRING) = "RAIL", "RAIL:1"',
            "0x200": 'WM_CLASS(STRING) = "vemu", "vemu"',
            "0x300": 'WM_CLASS(STRING) = "RAIL", "RAIL:2"',
        }

        def fake_run(command, **kwargs):
            if command[-1] == "_NET_CLIENT_LIST":
                return root
            window = command[2]
            cls = classes.get(window, "")
            return subprocess.CompletedProcess([], 0, cls, "")

        with mock.patch.object(
            watch.shutil, "which", return_value="/usr/bin/xprop"
        ), mock.patch.object(watch.subprocess, "run", side_effect=fake_run):
            self.assertEqual(watch.rail_windows(), 2)

    def test_no_xprop(self) -> None:
        from vemu.seamless import watch

        with mock.patch.object(watch.shutil, "which", return_value=None):
            self.assertEqual(watch.rail_windows(), 0)
