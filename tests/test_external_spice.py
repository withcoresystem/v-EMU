from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu.errors import ValidationError
from vemu.external_spice import (
    SpiceViewerManager,
    find_spiceviewer,
    viewer_command,
    viewer_endpoint,
    viewer_uri,
)


class ExternalSpiceCommandTests(unittest.TestCase):
    def test_command_shape(self) -> None:
        command = viewer_command(
            "/usr/bin/remote-viewer", {"host": "127.0.0.1", "port": 5900}
        )
        self.assertEqual(
            command,
            [
                "/usr/bin/remote-viewer",
                "--shared",
                "--auto-resize=never",
                "--spice-disable-usbredir",
                "--spice-disable-audio",
                "spice://127.0.0.1:5900",
            ],
        )

    def test_loopback_endpoints(self) -> None:
        self.assertEqual(
            viewer_endpoint({"host": "127.0.0.1", "port": 5900}),
            ("127.0.0.1", 5900),
        )
        self.assertEqual(
            viewer_endpoint({"host": "::1", "port": 5901}),
            ("::1", 5901),
        )
        self.assertEqual(
            viewer_endpoint({"host": "localhost", "port": 5902}),
            ("127.0.0.1", 5902),
        )
        self.assertEqual(
            viewer_endpoint({"port": 5903}),
            ("127.0.0.1", 5903),
        )

    def test_non_loopback_endpoints_are_refused(self) -> None:
        for host in ("0.0.0.0", "192.168.1.20", "example.com", "::"):
            with self.subTest(host=host):
                with self.assertRaises(ValidationError):
                    viewer_endpoint({"host": host, "port": 5900})

    def test_port_validation(self) -> None:
        for port in (0, 65536, "invalid", None):
            with self.subTest(port=port):
                with self.assertRaises(ValidationError):
                    viewer_endpoint({"host": "127.0.0.1", "port": port})

    def test_ipv6_uri(self) -> None:
        self.assertEqual(viewer_uri("::1", 5900), "spice://[::1]:5900")

    def test_find_remote_viewer(self) -> None:
        with mock.patch(
            "vemu.external_spice.shutil.which",
            return_value="/usr/bin/remote-viewer",
        ):
            self.assertEqual(
                find_spiceviewer(), "/usr/bin/remote-viewer"
            )
        with mock.patch(
            "vemu.external_spice.shutil.which", return_value=None
        ):
            self.assertIsNone(find_spiceviewer())


class FakeProcess:
    def __init__(self, command, **_kwargs) -> None:
        self.command = command
        self.kwargs = _kwargs
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


class ExternalSpiceProcessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.instances: list[FakeProcess] = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.log_path = Path(self.tmp.name) / "spice-viewer-vm1.log"

        def factory(command, **kwargs):
            process = FakeProcess(command, **kwargs)
            self.instances.append(process)
            return process

        self.popen = mock.patch(
            "vemu.external_spice.subprocess.Popen", side_effect=factory
        )
        self.popen.start()
        self.addCleanup(self.popen.stop)
        self.log = mock.patch(
            "vemu.external_spice.viewer_log_path",
            return_value=self.log_path,
        )
        self.log.start()
        self.addCleanup(self.log.stop)

    def test_missing_viewer(self) -> None:
        with mock.patch(
            "vemu.external_spice.find_spiceviewer", return_value=None
        ):
            with self.assertRaises(ValidationError) as ctx:
                SpiceViewerManager().launch(
                    "vm1", {"host": "127.0.0.1", "port": 5900}
                )
        self.assertIn("remote-viewer was not found", str(ctx.exception))
        self.assertIn("virt-viewer", str(ctx.exception))

    def test_duplicate_launch_is_refused(self) -> None:
        manager = SpiceViewerManager()
        handle = manager.launch(
            "vm1",
            {"host": "127.0.0.1", "port": 5900},
            viewer="/usr/bin/remote-viewer",
            settle_seconds=0,
        )
        self.assertTrue(manager.running("vm1"))
        self.assertIs(manager.handle("vm1"), handle)
        with self.assertRaises(ValidationError) as ctx:
            manager.launch(
                "vm1",
                {"host": "127.0.0.1", "port": 5900},
                viewer="/usr/bin/remote-viewer",
                settle_seconds=0,
            )
        self.assertIn("already open", str(ctx.exception))
        handle.process.returncode = 0
        self.assertFalse(manager.running("vm1"))
        manager.launch(
            "vm1",
            {"host": "127.0.0.1", "port": 5900},
            viewer="/usr/bin/remote-viewer",
            settle_seconds=0,
        )

    def test_immediate_exit_reports_log(self) -> None:
        manager = SpiceViewerManager()
        original = self.instances

        def failing_factory(command, **kwargs):
            process = FakeProcess(command, **kwargs)
            process.returncode = 2
            original.append(process)
            return process

        with mock.patch(
            "vemu.external_spice.subprocess.Popen",
            side_effect=failing_factory,
        ):
            with self.assertRaises(ValidationError) as ctx:
                manager.launch(
                    "vm1",
                    {"host": "127.0.0.1", "port": 5900},
                    viewer="/usr/bin/remote-viewer",
                    settle_seconds=0,
                )
        self.assertIn("exited immediately", str(ctx.exception))
        self.assertIn(str(self.log_path), str(ctx.exception))
        self.assertFalse(manager.running("vm1"))

    def test_start_failure_is_actionable(self) -> None:
        manager = SpiceViewerManager()
        with mock.patch(
            "vemu.external_spice.subprocess.Popen",
            side_effect=OSError("cannot execute"),
        ):
            with self.assertRaises(ValidationError) as ctx:
                manager.launch(
                    "vm1",
                    {"host": "127.0.0.1", "port": 5900},
                    viewer="/usr/bin/remote-viewer",
                    settle_seconds=0,
                )
        self.assertIn("Could not start remote-viewer", str(ctx.exception))
        self.assertFalse(manager.running("vm1"))

    def test_log_contains_fixed_flags(self) -> None:
        SpiceViewerManager().launch(
            "vm1",
            {"host": "127.0.0.1", "port": 5900},
            viewer="/usr/bin/remote-viewer",
            settle_seconds=0,
        )
        text = self.log_path.read_text(encoding="utf-8")
        self.assertIn("--shared", text)
        self.assertIn("--auto-resize=never", text)
        self.assertIn("--spice-disable-usbredir", text)
        self.assertIn("--spice-disable-audio", text)
        self.assertIn("spice://127.0.0.1:5900", text)


if __name__ == "__main__":
    unittest.main()
