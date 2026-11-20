from __future__ import annotations

import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from vemu import experimental3d
from vemu.cli import build_parser
from vemu.errors import Conflict, VemuError
from vemu.guest.transport import CommandResult

BASE_XML = """<domain type='kvm' xmlns:qemu='http://libvirt.org/schemas/domain/qemu/1.0'>
  <name>test</name>
  <memory unit='KiB'>4194304</memory>
  <currentMemory unit='KiB'>4194304</currentMemory>
  <vcpu placement='static'>2</vcpu>
  <os>
    <type arch='x86_64' machine='q35'>hvm</type>
    <loader readonly='yes' type='pflash'>/usr/share/vemu/firmware/x64/OVMF_CODE.fd</loader>
  </os>
  <devices>
    <graphics type='vnc' port='-1' autoport='yes' listen='127.0.0.1'/>
    <video>
      <model type='qxl' ram='65536' vram='65536' heads='1' primary='yes'/>
    </video>
  </devices>
</domain>
"""


class SeccompTests(unittest.TestCase):
    def test_parses_off_and_on(self) -> None:
        self.assertEqual(
            experimental3d.seccomp_value("seccomp_sandbox = 0\n"), "0"
        )
        self.assertEqual(
            experimental3d.seccomp_value('seccomp_sandbox = "off"\n'), "0"
        )
        self.assertEqual(
            experimental3d.seccomp_value("seccomp_sandbox = 1\n"), "1"
        )

    def test_missing_setting_defaults_on(self) -> None:
        self.assertEqual(experimental3d.seccomp_value(""), "1")


class SecureBootTests(unittest.TestCase):
    def test_loader_secure_is_enforced(self) -> None:
        xml = BASE_XML.replace("<loader ", "<loader secure='yes' ")
        self.assertTrue(experimental3d.secure_boot_enabled(xml))

    def test_loader_not_secure(self) -> None:
        self.assertFalse(experimental3d.secure_boot_enabled(BASE_XML))

    def test_explicit_enrolled_keys_win(self) -> None:
        xml = BASE_XML.replace(
            "<loader ",
            "<firmware><feature enabled='no' name='enrolled-keys'/></firmware>"
            "<loader secure='yes' ",
        )
        self.assertFalse(experimental3d.secure_boot_enabled(xml))


class ApplyXmlTests(unittest.TestCase):
    def test_switches_to_spice_and_virtio_vga_gl(self) -> None:
        updated = experimental3d.apply_xml(BASE_XML)
        root = ET.fromstring(updated)
        graphics = root.findall("devices/graphics")
        self.assertEqual(
            [item.get("type") for item in graphics],
            ["spice", "egl-headless"],
        )
        self.assertEqual(
            graphics[0].find("listen").get("address"), "127.0.0.1"
        )
        self.assertTrue(graphics[1].find("gl").get("rendernode"))
        model = root.find("devices/video/model")
        self.assertEqual(model.get("device"), "virtio-vga-gl")
        self.assertEqual(model.get("type"), "virtio")
        self.assertEqual(
            model.find("acceleration").get("accel3d"), "yes"
        )
        memory = root.find("memoryBacking")
        self.assertIsNotNone(memory)
        self.assertEqual(memory.find("source").get("type"), "memfd")
        self.assertEqual(memory.find("access").get("mode"), "shared")
        args = [
            item.get("value")
            for item in root.findall(
                "{http://libvirt.org/schemas/domain/qemu/1.0}commandline/"
                "{http://libvirt.org/schemas/domain/qemu/1.0}arg"
            )
        ]
        self.assertIn("virtio-vga-gl.blob=true", args)
        self.assertIn("virtio-vga-gl.hostmem=4G", args)
        self.assertIn("virtio-vga-gl.venus=true", args)

    def test_is_idempotent(self) -> None:
        once = experimental3d.apply_xml(BASE_XML)
        twice = experimental3d.apply_xml(once)
        root = ET.fromstring(twice)
        self.assertEqual(len(root.findall("memoryBacking")), 1)
        self.assertEqual(len(root.findall("devices/graphics")), 2)
        self.assertEqual(len(root.findall("devices/video")), 1)
        self.assertEqual(
            len(
                root.findall(
                    "{http://libvirt.org/schemas/domain/qemu/1.0}commandline"
                )
            ),
            1,
        )


class GateTests(unittest.TestCase):
    def test_gate_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "experimental.json"
            with mock.patch.dict(
                os.environ, {"VEMU_EXPERIMENTAL_CONFIG": str(path)}
            ):
                self.assertFalse(experimental3d.experimental_enabled())
                experimental3d.set_experimental_enabled(True)
                self.assertTrue(experimental3d.experimental_enabled())
                experimental3d.set_experimental_enabled(False)
                self.assertFalse(experimental3d.experimental_enabled())

    def test_gate_defaults_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            with mock.patch.dict(
                os.environ, {"VEMU_EXPERIMENTAL_CONFIG": str(path)}
            ):
                self.assertFalse(experimental3d.experimental_enabled())
                self.assertFalse(path.exists())


class StateTests(unittest.TestCase):
    def test_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"domain": "vm1", "enabled": True, "layer": "0002"}
            path = experimental3d._write_state("vm1", payload, tmp)
            self.assertTrue(path.is_file())
            self.assertEqual(
                experimental3d._load_state("vm1", tmp), payload
            )


class StateListTests(unittest.TestCase):
    def test_lists_shared_state_without_touching_vms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.dict(
                os.environ, {"VEMU_EXPERIMENTAL_STATE_DIR": str(root)}
            ):
                experimental3d._write_state(
                    "vm1",
                    {
                        "domain": "vm1",
                        "enabled": True,
                        "status": "enabled",
                        "host_changes": {"backup": "/tmp/qemu.conf.bak"},
                    },
                )
                states = experimental3d.list_states()
        self.assertEqual(states[0]["domain"], "vm1")
        self.assertTrue(states[0]["enabled"])
        self.assertTrue(states[0]["host_security_change"])


class HostSecurityRestoreTests(unittest.TestCase):
    def test_refuses_while_a_3d_state_is_enabled(self) -> None:
        with mock.patch.object(
            experimental3d,
            "list_states",
            return_value=[{"domain": "vm1", "enabled": True}],
        ):
            with self.assertRaises(Conflict):
                experimental3d.restore_host_security()

    def test_uses_secure_default_when_backup_is_missing(self) -> None:
        class FakeBackend:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def list_domains(self):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "missing.conf"
            with mock.patch.object(
                experimental3d, "list_states", return_value=[]
            ), mock.patch.object(
                experimental3d, "seccomp_value", return_value="0"
            ), mock.patch.object(
                experimental3d, "QEMU_CONF_BACKUP", backup
            ), mock.patch.object(
                experimental3d, "LibvirtBackend", FakeBackend
            ), mock.patch.object(
                experimental3d,
                "_set_seccomp_default",
                return_value={"changed": True, "fallback": True},
            ) as fallback:
                result = experimental3d.restore_host_security()
        fallback.assert_called_once()
        self.assertTrue(result["fallback"])

    def test_refuses_running_vms(self) -> None:
        class FakeBackend:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def list_domains(self):
                from types import SimpleNamespace

                return [SimpleNamespace(name="vm1", state="running")]

        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "qemu.conf.bak"
            backup.write_text("seccomp_sandbox = 1\n")
            with mock.patch.object(
                experimental3d, "list_states", return_value=[]
            ), mock.patch.object(
                experimental3d, "seccomp_value", return_value="0"
            ), mock.patch.object(
                experimental3d, "QEMU_CONF_BACKUP", backup
            ), mock.patch.object(
                experimental3d, "LibvirtBackend", FakeBackend
            ):
                with self.assertRaises(Conflict):
                    experimental3d.restore_host_security()

    def test_restores_when_safe(self) -> None:
        class FakeBackend:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def list_domains(self):
                return []

        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "qemu.conf.bak"
            backup.write_text("seccomp_sandbox = 1\n")
            with mock.patch.object(
                experimental3d, "list_states", return_value=[]
            ), mock.patch.object(
                experimental3d, "seccomp_value", return_value="0"
            ), mock.patch.object(
                experimental3d, "QEMU_CONF_BACKUP", backup
            ), mock.patch.object(
                experimental3d, "LibvirtBackend", FakeBackend
            ), mock.patch.object(
                experimental3d,
                "restore_seccomp",
                return_value={"changed": True, "restored": str(backup)},
            ) as restore:
                result = experimental3d.restore_host_security()
        restore.assert_called_once()
        self.assertTrue(result["changed"])
        self.assertEqual(result["seccomp_sandbox"], "1")


class FakeSSH:
    def __init__(
        self,
        responses: list[str],
        *,
        download_data: bytes = b"",
        run_response: str = "",
    ) -> None:
        self.responses = list(responses)
        self.download_data = download_data
        self.run_response = run_response
        self.scripts: list[str] = []
        self.commands: list[str] = []
        self.uploads: list[tuple[bytes, str]] = []

    def run_powershell(
        self, script: str, *, timeout: int = 120
    ) -> CommandResult:
        self.scripts.append(script)
        text = self.responses.pop(0) if self.responses else "{}"
        return CommandResult(exitcode=0, stdout=text, stderr="")

    def run(self, command: str, *, timeout: int = 60) -> CommandResult:
        self.commands.append(command)
        return CommandResult(
            exitcode=0, stdout=self.run_response, stderr=""
        )

    def upload(self, data: bytes, remote: str) -> None:
        self.uploads.append((data, remote))

    def download(
        self, remote: str, *, max_bytes: int = 4 * 1024 * 1024
    ) -> bytes:
        return self.download_data


class GuestDriverTests(unittest.TestCase):
    def test_verify_driver_matches_pin(self) -> None:
        ssh = FakeSSH(
            [
                json.dumps(
                    [
                        {
                            "Name": "vgpu VirtIO GPU 3D (WDDM)",
                            "DriverVersion": experimental3d.DRIVER_VERSION,
                        }
                    ]
                )
            ]
        )
        result = experimental3d._verify_driver(ssh)
        self.assertTrue(result["matches_pin"])
        self.assertIn("vgpu", result["adapter"])

    def test_verify_driver_rejects_basic_display(self) -> None:
        ssh = FakeSSH(
            [
                json.dumps(
                    [
                        {
                            "Name": "Microsoft Basic Display Adapter",
                            "DriverVersion": "10.0.1",
                        }
                    ]
                )
            ]
        )
        with self.assertRaises(VemuError):
            experimental3d._verify_driver(ssh)

    def test_verify_driver_reports_code_37(self) -> None:
        ssh = FakeSSH(
            [
                json.dumps(
                    [
                        {
                            "Name": "vgpu VirtIO GPU 3D (WDDM)",
                            "DriverVersion": experimental3d.DRIVER_VERSION,
                            "Status": "Error",
                            "ConfigManagerErrorCode": 37,
                        }
                    ]
                )
            ]
        )
        with self.assertRaises(experimental3d.DriverNotReady) as caught:
            experimental3d._verify_driver(ssh)
        self.assertEqual(caught.exception.problem_code, 37)

    def test_repair_boot_guard_resets_guard_and_restarts_device(self) -> None:
        ssh = FakeSSH(
            [
                '{"VgpuBootGuard":1}',
            ]
        )
        result = experimental3d._repair_boot_guard(ssh)
        self.assertTrue(result["repaired"])
        script = ssh.scripts[-1]
        self.assertIn("VgpuBootGuard", script)
        self.assertIn("pnputil /restart-device", script)
        self.assertIn("VioGpu3D", script)

    def test_install_driver_reports_exit_code(self) -> None:
        ssh = FakeSSH(
            [
                "",
                "exit=3010",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            installer = Path(tmp) / "vgpu3d-setup.exe"
            installer.write_bytes(b"stub")
            code = experimental3d._install_driver(ssh, installer)
        self.assertEqual(code, 3010)
        self.assertEqual(len(ssh.uploads), 1)
        self.assertIn("exit=", ssh.scripts[-1])

    def test_install_driver_applies_certutil_workaround(self) -> None:
        original = (
            "Import-Certificate -FilePath $cer "
            "-CertStoreLocation Cert:\\LocalMachine\\Root | Out-Null\n"
            "Import-Certificate -FilePath $cer "
            "-CertStoreLocation Cert:\\LocalMachine\\TrustedPublisher "
            "| Out-Null\n"
        )
        ssh = FakeSSH(
            ["", "exit=1"],
            download_data=original.encode("utf-8"),
            run_response="EXIT=0",
        )
        with tempfile.TemporaryDirectory() as tmp:
            installer = Path(tmp) / "vgpu3d-setup.exe"
            installer.write_bytes(b"stub")
            code = experimental3d._install_driver(ssh, installer)
        self.assertEqual(code, 0)
        patched = ssh.uploads[-1][0].decode("utf-8")
        self.assertIn("certutil.exe -addstore -f Root", patched)
        self.assertIn(
            "certutil.exe -addstore -f TrustedPublisher", patched
        )
        self.assertNotIn("Import-Certificate", patched)
        self.assertTrue(any("install.ps1" in item for item in ssh.commands))

    def test_patch_install_script_is_a_noop_without_import(self) -> None:
        ssh = FakeSSH([], download_data=b"Write-Host 'nothing to do'")
        self.assertFalse(experimental3d._patch_install_script(ssh))
        self.assertEqual(ssh.uploads, [])


class ParserTests(unittest.TestCase):
    def test_parses_enable(self) -> None:
        args = build_parser().parse_args(
            [
                "experimental",
                "3d",
                "enable",
                "vm1",
                "--username",
                "ProUser",
                "--yes",
                "--accept-host-security-change",
            ]
        )
        self.assertEqual(args.domain, "vm1")
        self.assertEqual(args.username, "ProUser")
        self.assertTrue(args.yes)
        self.assertTrue(args.accept_host_security_change)

    def test_parses_status_direct(self) -> None:
        args = build_parser().parse_args(
            ["experimental", "3d", "status", "vm1", "--direct"]
        )
        self.assertTrue(args.direct)

    def test_parses_repair(self) -> None:
        args = build_parser().parse_args(
            ["experimental", "3d", "repair", "vm1", "--yes"]
        )
        self.assertEqual(args.domain, "vm1")
        self.assertTrue(args.yes)


if __name__ == "__main__":
    unittest.main()
