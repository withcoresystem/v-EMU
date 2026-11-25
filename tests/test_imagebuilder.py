from __future__ import annotations

import json
import os
from dataclasses import replace
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from vemu.errors import ValidationError
from vemu.imagebuilder import (
    BuildJob,
    FakeImageBuilderAdapter,
    HostStatus,
    ImageRecord,
    PreflightCheck,
    RealImageBuilderAdapter,
)


class _FakeManifestProcess:
    def __init__(self, code: int, stdout: str, stderr: str) -> None:
        self.returncode = code
        self.pid = 999999
        self._stdout = stdout
        self._stderr = stderr

    def communicate(self, timeout=None):
        return self._stdout, self._stderr

    def poll(self):
        return self.returncode


class RealAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.blueprint = Path(self.tmp.name) / "desktop.toml"
        self.blueprint.write_text(
            'name = "Fedora KDE"\n'
            'version = "1.0.0"\n'
            'distro = "fedora-45"\n'
            '[[customizations.repositories]]\n'
            'id = "rpmfusion-free"\n'
            'baseurls = ["https://example.invalid/free/"]\n'
            'enabled = true\n'
            '[[customizations.firstboot.scripts]]\n'
            'type = "custom"\n'
            'contents = "true"\n',
            encoding="utf-8",
        )
        self.adapter = RealImageBuilderAdapter()
        self.record = self.adapter.import_blueprint(self.blueprint)

    def test_import_stores_absolute_blueprint_path(self) -> None:
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.tmp.name)
        record = self.adapter.import_blueprint("desktop.toml")
        self.assertTrue(Path(record.path).is_absolute())
        self.assertEqual(Path(record.path), self.blueprint.resolve())

    def test_host_status_accepts_libexec_depsolve(self) -> None:
        def fake_tool(name: str) -> str:
            if name == "osbuild-depsolve-dnf":
                return "/usr/libexec/osbuild-depsolve-dnf"
            return f"/usr/bin/{name}"

        with mock.patch(
            "vemu.imagebuilder._tool_path", side_effect=fake_tool
        ), mock.patch.object(
            self.adapter, "_version", return_value="89.0.0"
        ):
            status = self.adapter.host_status()
        self.assertTrue(status.ready)
        self.assertIn("89.0.0", status.summary)

    def test_version_falls_back_to_version_flag(self) -> None:
        def fake_run(command, **_kwargs):
            if command[1] == "version":
                return subprocess.CompletedProcess(
                    command, 1, "", 'error: unknown command "version"'
                )
            return subprocess.CompletedProcess(
                command,
                0,
                'image-builder:\n  version: "52.1"\n  commit: unknown\n',
                "",
            )

        with mock.patch(
            "vemu.imagebuilder.subprocess.run", side_effect=fake_run
        ):
            version = RealImageBuilderAdapter._version(
                "/usr/bin/image-builder"
            )
        self.assertEqual(version, "52.1")

    def test_image_type_mapping(self) -> None:
        self.assertEqual(
            RealImageBuilderAdapter.image_type("qcow2", "fedora-45"),
            "generic-qcow2",
        )
        self.assertEqual(
            RealImageBuilderAdapter.image_type("iso", "fedora-45"),
            "minimal-installer",
        )
        self.assertEqual(
            RealImageBuilderAdapter.image_type("qcow2", "almalinux-10.2"),
            "qcow2",
        )
        self.assertEqual(
            RealImageBuilderAdapter.image_type("iso", "almalinux-10.2"),
            "image-installer",
        )
        with self.assertRaises(ValidationError):
            RealImageBuilderAdapter.image_type("qcow2", "debian-13")

    def test_extra_repos_and_build_command(self) -> None:
        self.assertEqual(
            self.adapter.extra_repos(self.record),
            ["https://example.invalid/free/"],
        )
        with mock.patch(
            "vemu.imagebuilder.shutil.which",
            return_value="/usr/bin/image-builder",
        ):
            command = self.adapter.build_command(
                "qcow2", self.record, self.tmp.name
            )
        self.assertEqual(command[0], "/usr/bin/image-builder")
        self.assertIn("generic-qcow2", command)
        self.assertIn("--extra-repo", command)
        self.assertIn("https://example.invalid/free/", command)

    def test_preflight_manifest_success(self) -> None:
        with mock.patch.object(
            self.adapter,
            "host_status",
            return_value=HostStatus(True, "Fedora 45 · image-builder 87"),
        ), mock.patch.object(
            self.adapter,
            "_selinux_preflight",
            return_value=PreflightCheck("SELinux", "ok", "test"),
        ), mock.patch(
            "vemu.imagebuilder.shutil.which",
            return_value="/usr/bin/image-builder",
        ), mock.patch(
            "vemu.imagebuilder.subprocess.Popen",
            return_value=_FakeManifestProcess(
                0,
                json.dumps(
                    {
                        "sources": {
                            "org.osbuild.librepo": {
                                "items": {"sha256:a": {}, "sha256:b": {}}
                            }
                        }
                    }
                ),
                "",
            ),
        ):
            result = self.adapter.preflight(
                "qcow2", self.record, self.tmp.name
            )
        self.assertFalse(result.blocking)
        self.assertEqual(result.packages, 2)
        self.assertTrue(
            any(c.label == "Packages and repos" and c.state == "ok"
                for c in result.checks)
        )
        self.assertTrue(
            any(c.label == "Firstboot" and "1 script" in c.detail
                for c in result.checks)
        )

    def test_selinux_preflight_uses_images_role_label(self) -> None:
        from vemu.storage import Location

        location = Location(
            path=self.tmp.name,
            role="images",
            exists=True,
            filesystem="xfs",
            total_bytes=1024**3,
            free_bytes=512 * 1024**3,
            selinux_mode="Enforcing",
            selinux_label="virt_image_t",
            expected_label="virt_image_t",
            owner="user:user",
            mode="750",
            nodatacow=False,
            qemu_traverse=True,
            qemu_read=True,
            qemu_write=False,
        )
        with mock.patch(
            "vemu.storage.inspect_location", return_value=location
        ):
            check = self.adapter._selinux_preflight(self.tmp.name)
        self.assertEqual(check.state, "ok")
        self.assertIn("virt_image_t", check.detail)

    def test_rhel_target_blocks_on_non_rhel_host(self) -> None:
        self.adapter.repo_dir = None
        record = replace(self.record, distro="rhel-10.3")
        with mock.patch.object(
            self.adapter,
            "host_status",
            return_value=HostStatus(True, "Fedora 45 · image-builder 89"),
        ), mock.patch.object(
            self.adapter,
            "_selinux_preflight",
            return_value=PreflightCheck("SELinux", "ok", "test"),
        ), mock.patch(
            "vemu.imagebuilder._os_release",
            return_value={
                "ID": "fedora",
                "PRETTY_NAME": "Fedora Linux 44",
            },
        ), mock.patch(
            "vemu.imagebuilder.shutil.which",
            return_value="/usr/bin/image-builder",
        ), mock.patch(
            "vemu.imagebuilder.subprocess.Popen",
            return_value=_FakeManifestProcess(
                1, "", "This system does not have any valid subscriptions"
            ),
        ):
            result = self.adapter.preflight(
                "qcow2", record, self.tmp.name
            )
        checks = {check.label: check for check in result.checks}
        self.assertEqual(checks["RHEL subscription"].state, "error")
        self.assertIn("RHEL", checks["RHEL subscription"].detail)

    def test_preflight_manifest_failure_blocks(self) -> None:
        with mock.patch.object(
            self.adapter,
            "host_status",
            return_value=HostStatus(True, "Fedora 45 · image-builder 87"),
        ), mock.patch.object(
            self.adapter,
            "_selinux_preflight",
            return_value=PreflightCheck("SELinux", "ok", "test"),
        ), mock.patch(
            "vemu.imagebuilder.shutil.which",
            return_value="/usr/bin/image-builder",
        ), mock.patch(
            "vemu.imagebuilder.subprocess.Popen",
            return_value=_FakeManifestProcess(
                1, "", "No match for argument: missing-package"
            ),
        ):
            result = self.adapter.preflight(
                "qcow2", self.record, self.tmp.name
            )
        self.assertTrue(result.blocking)
        self.assertTrue(
            any(
                c.label == "Packages and repos"
                and c.state == "error"
                and "missing-package" in c.detail
                for c in result.checks
            )
        )

    def test_manifest_cancel_stops_process(self) -> None:
        cancel = threading.Event()
        cancel.set()
        code, _stdout, stderr = RealImageBuilderAdapter._run_manifest(
            ["/bin/sh", "-c", "sleep 30"], cancel=cancel
        )
        self.assertNotEqual(code, 0)
        self.assertIn("cancelled", stderr)

    def test_repo_dir_is_passed_to_build_command(self) -> None:
        adapter = RealImageBuilderAdapter(repo_dir=self.tmp.name)
        with mock.patch(
            "vemu.imagebuilder.shutil.which",
            return_value="/usr/bin/image-builder",
        ):
            command = adapter.build_command(
                "qcow2", self.record, self.tmp.name
            )
        self.assertIn("--force-repo-dir", command)
        self.assertIn(self.tmp.name, command)

    def test_start_build_uses_pkexec_and_job_dir(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch(
            "vemu.imagebuilder.os.geteuid", return_value=1000
        ), mock.patch(
            "vemu.imagebuilder.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}",
        ), mock.patch(
            "vemu.imagebuilder.subprocess.Popen", return_value=process
        ) as popen:
            job = self.adapter.start_build(
                "qcow2",
                self.record,
                self.tmp.name,
                expected_packages=1349,
            )
        self.assertEqual(job.state, "Running")
        self.assertEqual(job.expected_packages, 1349)
        self.assertIs(job.process, process)
        self.assertEqual(job.command[0], "/usr/bin/pkexec")
        self.assertIn("build", job.command)
        self.assertTrue(job.log_path.parent.is_dir())
        self.assertTrue(job.log_path.is_file())
        self.assertIn("--output-dir", popen.call_args.args[0])


class RealAdapterCatalogTests(unittest.TestCase):
    def _adapter(self) -> RealImageBuilderAdapter:
        return RealImageBuilderAdapter()

    def test_catalog_parses_image_builder_list(self) -> None:
        payload = [
            {
                "distro": {"name": "fedora-45"},
                "arch": {"name": "x86_64"},
                "image_type": {"name": "generic-qcow2"},
            },
            {
                "distro": {"name": "almalinux-10.2"},
                "arch": {"name": "x86_64"},
                "image_type": {"name": "qcow2"},
            },
        ]
        result = mock.Mock(
            returncode=0, stdout=json.dumps(payload), stderr=""
        )
        with mock.patch(
            "vemu.imagebuilder.shutil.which",
            return_value="/usr/bin/image-builder",
        ), mock.patch(
            "vemu.imagebuilder.subprocess.run", return_value=result
        ):
            entries = self._adapter().catalog()
        self.assertEqual(
            entries,
            [
                {
                    "distro": "almalinux-10.2",
                    "arch": "x86_64",
                    "image_type": "qcow2",
                },
                {
                    "distro": "fedora-45",
                    "arch": "x86_64",
                    "image_type": "generic-qcow2",
                },
            ],
        )

    def test_catalog_is_cached_until_refresh(self) -> None:
        payload = [
            {
                "distro": {"name": "fedora-45"},
                "arch": {"name": "x86_64"},
                "image_type": {"name": "generic-qcow2"},
            }
        ]
        result = mock.Mock(
            returncode=0, stdout=json.dumps(payload), stderr=""
        )
        with mock.patch(
            "vemu.imagebuilder.shutil.which",
            return_value="/usr/bin/image-builder",
        ), mock.patch(
            "vemu.imagebuilder.subprocess.run", return_value=result
        ) as run:
            adapter = self._adapter()
            first = adapter.catalog()
            second = adapter.catalog()
            refreshed = adapter.catalog(refresh=True)
        self.assertEqual(first, second)
        self.assertEqual(refreshed, first)
        self.assertEqual(run.call_count, 2)

    def test_catalog_reports_missing_tool(self) -> None:
        with mock.patch(
            "vemu.imagebuilder.shutil.which", return_value=None
        ):
            with self.assertRaises(ValidationError):
                self._adapter().catalog()

    def test_catalog_reports_command_failure(self) -> None:
        result = mock.Mock(returncode=1, stdout="", stderr="catalog boom")
        with mock.patch(
            "vemu.imagebuilder.shutil.which",
            return_value="/usr/bin/image-builder",
        ), mock.patch(
            "vemu.imagebuilder.subprocess.run", return_value=result
        ):
            with self.assertRaises(ValidationError) as raised:
                self._adapter().catalog()
        self.assertIn("catalog boom", str(raised.exception))



class BuildJobTests(unittest.TestCase):
    def test_successful_process_updates_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "build.log"
            with log_path.open("wb") as log:
                process = subprocess.Popen(
                    ["/bin/sh", "-c", "echo hello; exit 0"],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            image = ImageRecord(
                name="test",
                target="fedora-45 / qcow2",
                state="Running",
                created="now",
                size="—",
                path=tmp,
            )
            job = BuildJob(
                image=image,
                command=["/bin/sh"],
                log_path=log_path,
                process=process,
                state="Running",
            )
            for _ in range(50):
                if job.poll() == "Succeeded":
                    break
                time.sleep(0.02)
            self.assertEqual(job.state, "Succeeded")
            self.assertEqual(image.state, "Succeeded")
            self.assertTrue(log_path.is_file())

    def test_success_cleans_intermediates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            artifact = root / "final.iso"
            artifact.write_bytes(b"iso")
            log_path = root / "build.log"
            log_path.write_text("log", encoding="utf-8")
            (root / "base").mkdir()
            (root / "base" / "base.iso").write_bytes(b"base")
            (root / "customizer").mkdir()
            (root / "updates.img").write_bytes(b"updates")
            image = ImageRecord(
                name="test",
                target="fedora-45 / iso",
                state="Running",
                created="now",
                size="—",
                path=str(root),
            )
            job = BuildJob(
                image=image,
                command=["/bin/true"],
                log_path=log_path,
                state="Running",
            )
            job._finish("Succeeded")
            self.assertEqual(
                sorted(item.name for item in root.iterdir()),
                ["build.log", "final.iso"],
            )
            self.assertEqual(job.image.path, str(artifact))

    def test_cancel_marks_job_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "build.log"
            with log_path.open("wb") as log:
                process = subprocess.Popen(
                    ["/bin/sh", "-c", "sleep 30"],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            image = ImageRecord(
                name="test",
                target="fedora-45 / iso",
                state="Running",
                created="now",
                size="—",
                path=tmp,
            )
            job = BuildJob(
                image=image,
                command=["/bin/sh"],
                log_path=log_path,
                process=process,
                state="Running",
            )
            job.cancel()
            self.assertEqual(job.state, "Cancelled")
            self.assertEqual(image.state, "Cancelled")


class FakeAdapterStillWorksTests(unittest.TestCase):
    def test_fake_adapter_remains_usable(self) -> None:
        adapter = FakeImageBuilderAdapter()
        self.assertTrue(adapter.host_status().ready)
        self.assertTrue(adapter.host_status().summary)


if __name__ == "__main__":
    unittest.main()
