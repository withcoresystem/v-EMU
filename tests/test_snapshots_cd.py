from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vemu import service
from vemu.chain import ChainManager
from vemu.errors import Conflict

XML = """<domain type='kvm'>
  <devices>
    <disk type='file' device='disk'>
      <source file='/labs/disk-0001.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <disk type='file' device='cdrom'>
      <source file='/isos/installer.iso'/>
      <target dev='sdb' bus='sata'/>
      <boot order='1'/>
    </disk>
    <disk type='file' device='cdrom'>
      <source file='/isos/tools.iso'/>
      <target dev='sdc' bus='sata'/>
    </disk>
    <disk type='file' device='cdrom'>
      <target dev='sdd' bus='sata'/>
    </disk>
  </devices>
</domain>"""


class FakeBackend:
    def __init__(self, xml: str = XML) -> None:
        self.xml = xml
        self.redefines: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def config_xml(self, _name: str) -> str:
        return self.xml

    def redefine_domain(self, _name: str, xml: str) -> None:
        self.xml = xml
        self.redefines.append(xml)

    def update_disk_sources(self, _name: str, _changes: dict) -> None:
        return None


class FakeManager:
    def __init__(self, _path: Path) -> None:
        self.snapshot_calls: list[str] = []
        self.description_calls: list[str] = []
        self.revert_calls: list[str] = []

    def snapshot(self, label: str, description: str = ""):
        self.snapshot_calls.append(label)
        self.description_calls.append(description)
        return SimpleNamespace(
            to_dict=lambda: {
                "id": "0002",
                "label": label,
                "description": description,
            }
        )

    def load(self):
        return SimpleNamespace()

    def info(self) -> dict:
        return {"active": "0002", "layers": []}

    def revert(self, layer: str, *, cleanup: bool = True) -> None:
        self.revert_calls.append(layer)

    def cleanup_deferred(self) -> None:
        return None


class FailingManager(FakeManager):
    def snapshot(self, label: str, description: str = ""):
        raise RuntimeError("snapshot failed")


class SnapshotCdTests(unittest.TestCase):
    def test_eject_all_cdroms_removes_sources_and_boot(self) -> None:
        backend = FakeBackend()
        ejected = service._eject_all_cdroms(backend, "vm1")
        self.assertEqual(ejected, ["sdb", "sdc"])
        self.assertEqual(len(backend.redefines), 1)
        root = ET.fromstring(backend.xml)
        disks = {
            disk.find("target").get("dev"): disk
            for disk in root.findall("./devices/disk")
        }
        self.assertEqual(
            disks["vda"].find("source").get("file"),
            "/labs/disk-0001.qcow2",
        )
        for dev in ("sdb", "sdc"):
            self.assertIsNone(disks[dev].find("source"))
            self.assertIsNone(disks[dev].find("boot"))
        self.assertIsNone(disks["sdd"].find("source"))

    def test_no_cd_does_not_redefine(self) -> None:
        backend = FakeBackend(
            "<domain><devices>"
            "<disk type='file' device='disk'>"
            "<source file='/labs/disk-0001.qcow2'/>"
            "<target dev='vda'/>"
            "</disk></devices></domain>"
        )
        self.assertEqual(service._eject_all_cdroms(backend, "vm1"), [])
        self.assertEqual(backend.redefines, [])

    def test_snapshot_description_is_passed_and_normalized(self) -> None:
        backend = FakeBackend()
        manager = FakeManager(Path("/tmp/chain"))

        class FakeBackendContext:
            def __enter__(self):
                return backend

            def __exit__(self, *_args):
                return False

        with mock.patch.object(
            service, "LibvirtBackend", return_value=FakeBackendContext()
        ), mock.patch.object(
            service, "ChainManager", return_value=manager
        ), mock.patch.object(
            service,
            "_require_stopped",
            return_value=Path("/tmp/chain"),
        ), mock.patch.object(
            service,
            "_chain_source_changes",
            return_value={},
        ), mock.patch.object(
            service,
            "_eject_all_cdroms",
            return_value=[],
        ), mock.patch.object(
            service, "_relabel", return_value=None
        ):
            result = service.snapshots_create(
                "vm1", "before-upgrade", description="line 1\r\nline 2"
            )
        self.assertEqual(manager.description_calls, ["line 1\nline 2"])
        self.assertEqual(
            result["layer"]["description"], "line 1\nline 2"
        )

    def test_snapshot_update_reaches_manager(self) -> None:
        backend = FakeBackend()
        manager = FakeManager(Path("/tmp/chain"))
        calls: list[tuple[str, dict]] = []

        def update(layer: str, **kwargs):
            calls.append((layer, kwargs))
            return SimpleNamespace(
                to_dict=lambda: {
                    "id": layer,
                    "label": kwargs.get("label", ""),
                    "description": kwargs.get("description", ""),
                }
            )

        manager.update_snapshot = update  # type: ignore[attr-defined]

        class FakeBackendContext:
            def __enter__(self):
                return backend

            def __exit__(self, *_args):
                return False

        with mock.patch.object(
            service, "LibvirtBackend", return_value=FakeBackendContext()
        ), mock.patch.object(
            service, "ChainManager", return_value=manager
        ), mock.patch.object(
            service, "_require_writable"
        ), mock.patch.object(
            service,
            "_chain_dir_for_domain",
            return_value=Path("/tmp/chain"),
        ), mock.patch.object(
            service, "_relabel", return_value=None
        ):
            result = service.snapshots_update(
                "vm1",
                "0002",
                label="before-upgrade",
                description="Notes",
            )
        self.assertEqual(
            calls, [("0002", {"label": "before-upgrade", "description": "Notes"})]
        )
        self.assertEqual(result["layer"]["id"], "0002")

    def test_snapshot_description_validation(self) -> None:
        with self.assertRaises(service.ValidationError):
            service.snapshots_create(
                "vm1", "too-long", description="x" * 4097
            )
        with self.assertRaises(service.ValidationError):
            service.snapshots_create(
                "vm1", "control", description="bad\x00value"
            )

    def test_snapshot_create_ejects_and_reports(self) -> None:
        backend = FakeBackend()
        manager = FakeManager(Path("/tmp/chain"))
        with (
            mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ),
            mock.patch(
                "vemu.service._require_stopped",
                return_value=Path("/tmp/chain"),
            ),
            mock.patch(
                "vemu.service.ChainManager", return_value=manager
            ),
            mock.patch(
                "vemu.service._chain_source_changes", return_value={}
            ),
            mock.patch("vemu.service._relabel"),
        ):
            result = service.snapshots_create("vm1", "before-update")
        self.assertEqual(result["ejected"], ["sdb", "sdc"])
        self.assertEqual(manager.snapshot_calls, ["before-update"])
        root = ET.fromstring(backend.xml)
        self.assertIsNone(
            root.findall("./devices/disk")[1].find("source")
        )

    def test_snapshot_failure_restores_media(self) -> None:
        backend = FakeBackend()
        manager = FailingManager(Path("/tmp/chain"))
        with (
            mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ),
            mock.patch(
                "vemu.service._require_stopped",
                return_value=Path("/tmp/chain"),
            ),
            mock.patch(
                "vemu.service.ChainManager", return_value=manager
            ),
            mock.patch(
                "vemu.service._chain_source_changes", return_value={}
            ),
            mock.patch("vemu.service._relabel"),
        ):
            with self.assertRaises(RuntimeError):
                service.snapshots_create("vm1", "fails")
        self.assertEqual(backend.xml, XML)

    def test_snapshot_preflight_blocks_duplicate_before_layer(self) -> None:
        backend = FakeBackend()
        manager = FakeManager(Path("/tmp/chain"))
        with (
            mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ),
            mock.patch(
                "vemu.service._require_stopped",
                return_value=Path("/tmp/chain"),
            ),
            mock.patch(
                "vemu.service.ChainManager", return_value=manager
            ),
            mock.patch(
                "vemu.service._chain_source_changes",
                side_effect=Conflict("duplicate chain disk"),
            ),
        ):
            with self.assertRaises(Conflict):
                service.snapshots_create("vm1", "blocked")
        self.assertEqual(manager.snapshot_calls, [])
        self.assertEqual(backend.xml, XML)
        self.assertEqual(backend.redefines, [])

    def test_revert_ejects_media(self) -> None:
        backend = FakeBackend()
        manager = FakeManager(Path("/tmp/chain"))
        with (
            mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ),
            mock.patch(
                "vemu.service._require_stopped",
                return_value=Path("/tmp/chain"),
            ),
            mock.patch(
                "vemu.service.ChainManager", return_value=manager
            ),
            mock.patch(
                "vemu.service._chain_source_changes", return_value={}
            ),
            mock.patch("vemu.service._relabel"),
        ):
            result = service.snapshots_revert("vm1", "0001")
        self.assertEqual(result["ejected"], ["sdb", "sdc"])
        self.assertEqual(manager.revert_calls, ["0001"])


@unittest.skipUnless(shutil.which("qemu-img"), "qemu-img is required")
class RealChainServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        base = root / "base.qcow2"
        subprocess.run(
            [shutil.which("qemu-img"), "create", "-f", "qcow2", str(base), "16M"],
            check=True,
            capture_output=True,
        )
        self.chain_dir = root / "chain"
        ChainManager.create(self.chain_dir, base)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _xml(self, layer: str) -> str:
        return (
            "<domain><devices>"
            "<disk type='file' device='disk'>"
            f"<source file='{self.chain_dir / f'disk-vda-{layer}.qcow2'}'/>"
            "<target dev='vda'/>"
            "</disk></devices></domain>"
        )

    def _run(self, xml: str, action):
        backend = FakeBackend(xml)
        updates: list[dict[str, str]] = []

        def update(_name: str, changes: dict[str, str]) -> None:
            updates.append(dict(changes))

        backend.update_disk_sources = update  # type: ignore[method-assign]

        class Context:
            def __enter__(self):
                return backend

            def __exit__(self, *_args):
                return False

        with mock.patch.object(
            service, "LibvirtBackend", return_value=Context()
        ), mock.patch.object(
            service, "_require_stopped", return_value=self.chain_dir
        ), mock.patch.object(
            service, "_relabel", return_value=None
        ), mock.patch.object(
            service, "_eject_all_cdroms", return_value=[]
        ):
            payload = action()
        return payload, updates

    def test_snapshot_switches_domain_to_new_current_layer(self) -> None:
        payload, updates = self._run(
            self._xml("0001"),
            lambda: service.snapshots_create("vm1", "S1"),
        )
        self.assertEqual(payload["layer"]["id"], "0001")
        self.assertEqual(payload["active"], "0002")
        self.assertEqual(
            updates,
            [
                {
                    str(self.chain_dir / "disk-vda-0001.qcow2"): str(
                        self.chain_dir / "disk-vda-0002.qcow2"
                    )
                }
            ],
        )

    def test_revert_switches_domain_and_cleans_discarded_layer(self) -> None:
        self._run(
            self._xml("0001"),
            lambda: service.snapshots_create("vm1", "S1"),
        )
        payload, updates = self._run(
            self._xml("0002"),
            lambda: service.snapshots_revert("vm1", "0001"),
        )
        self.assertEqual(payload["active"], "0003")
        self.assertEqual(
            updates,
            [
                {
                    str(self.chain_dir / "disk-vda-0002.qcow2"): str(
                        self.chain_dir / "disk-vda-0003.qcow2"
                    )
                }
            ],
        )
        self.assertFalse(
            (self.chain_dir / "disk-vda-0002.qcow2").exists()
        )


if __name__ == "__main__":
    unittest.main()
