"""Host passthrough (VFIO) is detected and treated as read-only External."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu import diagnose, service
from vemu.backend import DomainInfo, LibvirtBackend
from vemu.errors import Conflict
from vemu.vmconfig import passthrough_devices, passthrough_summary

PCI_HOSTDEV = """
<hostdev mode="subsystem" type="pci" managed="yes">
  <source>
    <address domain="0x0000" bus="0x01" slot="0x00" function="0x0"/>
  </source>
</hostdev>
"""

USB_HOSTDEV = """
<hostdev mode="subsystem" type="usb" managed="yes">
  <source>
    <vendor id="0x046d"/>
    <product id="0xc52b"/>
  </source>
</hostdev>
"""

INTERFACE_HOSTDEV = """
<interface type="hostdev" managed="yes">
  <source>
    <address type="pci" domain="0x0000" bus="0x02" slot="0x10" function="0x1"/>
  </source>
</interface>
"""


def domain_xml(devices: str = "", *, metadata: str = "") -> str:
    return (
        "<domain>"
        "<name>vm1</name>"
        f"{metadata}"
        "<devices>"
        "<disk type='file' device='disk'>"
        "<source file='/tmp/disk.qcow2'/>"
        "<target dev='vda' bus='virtio'/>"
        "</disk>"
        f"{devices}"
        "</devices>"
        "</domain>"
    )


def full_metadata() -> str:
    return (
        "<metadata>"
        '<vemu:management xmlns:vemu="https://vemu.local/management" '
        'managed_by="v-EMU (full)" mode="full" origin="vemu"/>'
        "</metadata>"
    )


class PassthroughDetectionTests(unittest.TestCase):
    def test_pci_hostdev_is_passthrough(self) -> None:
        devices = passthrough_devices(domain_xml(PCI_HOSTDEV))
        self.assertEqual(devices, [{"kind": "pci", "source": "hostdev"}])
        self.assertEqual(
            passthrough_summary(domain_xml(PCI_HOSTDEV)), "pci x1"
        )

    def test_mediated_scsi_and_interface_hostdev(self) -> None:
        xml = domain_xml(
            '<hostdev mode="subsystem" type="mdev"><source>'
            "<address uuid='abc'/></source></hostdev>"
            '<hostdev mode="subsystem" type="scsi_host">'
            "<source protocol='vhost' wwpn='0x1'/></hostdev>"
            + INTERFACE_HOSTDEV
        )
        self.assertEqual(
            passthrough_summary(xml),
            "mdev x1, scsi_host x1, interface x1",
        )

    def test_usb_hostdev_is_not_passthrough(self) -> None:
        self.assertEqual(passthrough_devices(domain_xml(USB_HOSTDEV)), [])
        self.assertEqual(passthrough_summary(domain_xml(USB_HOSTDEV)), "")

    def test_invalid_xml_is_ignored(self) -> None:
        self.assertEqual(passthrough_devices("<domain>"), [])


class _FakeLibvirt:
    VIR_DOMAIN_XML_INACTIVE = 1

    class libvirtError(Exception):
        pass


class _FakeDomain:
    def __init__(self, live: str, config: str | None = None) -> None:
        self.live = live
        self.config = config if config is not None else live

    def XMLDesc(self, flags: int) -> str:
        return self.config if flags else self.live

    def autostart(self) -> int:
        return 0

    def name(self) -> str:
        return "vm1"

    def UUIDString(self) -> str:
        return "uuid-vm1"

    def state(self) -> tuple[int, int]:
        return (5, 0)


def describe(xml: str, *, config: str | None = None) -> DomainInfo:
    backend = object.__new__(LibvirtBackend)
    backend._libvirt = _FakeLibvirt()
    return backend._describe(_FakeDomain(xml, config))


class EffectiveModeTests(unittest.TestCase):
    def test_full_metadata_with_pci_becomes_external(self) -> None:
        info = describe(domain_xml(PCI_HOSTDEV, metadata=full_metadata()))
        self.assertEqual(info.managed_by, "External")
        self.assertTrue(info.passthrough)
        self.assertEqual(info.passthrough_detail, "pci x1")

    def test_usb_hostdev_keeps_full_metadata(self) -> None:
        info = describe(domain_xml(USB_HOSTDEV, metadata=full_metadata()))
        self.assertEqual(info.managed_by, "v-EMU (full)")
        self.assertFalse(info.passthrough)

    def test_removing_hostdev_restores_full_mode(self) -> None:
        with_hostdev = describe(
            domain_xml(PCI_HOSTDEV, metadata=full_metadata())
        )
        without = describe(domain_xml(metadata=full_metadata()))
        self.assertEqual(with_hostdev.managed_by, "External")
        self.assertEqual(without.managed_by, "v-EMU (full)")
        self.assertFalse(without.passthrough)

    def test_inactive_xml_drives_detection(self) -> None:
        # A live definition without the hostdev must not hide the config.
        info = describe(
            domain_xml(metadata=full_metadata()),
            config=domain_xml(PCI_HOSTDEV, metadata=full_metadata()),
        )
        self.assertTrue(info.passthrough)
        self.assertEqual(info.managed_by, "External")


class FakePassthroughBackend:
    def __init__(self, *, state: str = "stopped") -> None:
        self.state = state
        self.calls: list[str] = []
        self.xml = domain_xml(PCI_HOSTDEV, metadata=full_metadata())

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def info(self, name: str) -> DomainInfo:
        return DomainInfo(
            name=name,
            uuid="uuid",
            state=self.state,
            disks=["/tmp/disk.qcow2"],
            autostart=False,
            firmware="bios",
            managed_by="External",
            passthrough=True,
            passthrough_detail="pci x1",
        )

    def list_domains(self):
        return [self.info("vm1")]

    def config_xml(self, _name: str) -> str:
        return self.xml

    def snapshot_details(self, _name: str):
        return []

    def start(self, _name: str) -> None:
        self.calls.append("start")

    def shutdown(self, *_args, **_kwargs) -> None:
        self.calls.append("shutdown")

    def destroy(self, _name: str) -> None:
        self.calls.append("destroy")

    def redefine_domain(self, _name: str, xml: str) -> None:
        self.xml = xml
        self.calls.append("redefine")


class PassthroughGuardTests(unittest.TestCase):
    def test_lifecycle_is_read_only(self) -> None:
        backend = FakePassthroughBackend()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            with self.assertRaisesRegex(Conflict, "passthrough"):
                service.domain_start("vm1")
            with self.assertRaisesRegex(Conflict, "passthrough"):
                service.domain_shutdown("vm1")
            with self.assertRaisesRegex(Conflict, "passthrough"):
                service.domain_force_off("vm1")
        self.assertEqual(backend.calls, [])

    def test_hardware_update_is_read_only(self) -> None:
        backend = FakePassthroughBackend()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            with self.assertRaisesRegex(Conflict, "passthrough"):
                service.vm_config_update("vm1", memory_mib=4096)
        self.assertEqual(backend.calls, [])

    def test_partial_adoption_is_refused(self) -> None:
        backend = FakePassthroughBackend()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ):
                with self.assertRaisesRegex(Conflict, "passthrough"):
                    service.adopt_domain(
                        "vm1", mode="partial", backup_dir=tmp
                    )
        self.assertEqual(backend.calls, [])

    def test_rescue_start_stays_available(self) -> None:
        backend = FakePassthroughBackend()
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ):
                result = service.domain_rescue_start(
                    "vm1", backup_dir=tmp
                )
            self.assertTrue(result["started"])
            self.assertTrue(Path(result["backup"]).is_file())
        self.assertIn("start", backend.calls)

    def test_migration_plan_reports_passthrough(self) -> None:
        backend = FakePassthroughBackend()
        with tempfile.TemporaryDirectory() as tmp:
            missing = str(Path(tmp) / "storage.json")
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ):
                plan = service.vm_migration_plan(
                    "vm1", storage_config=missing
                )
        self.assertTrue(
            any("passthrough" in reason for reason in plan["reasons"])
        )

    def test_rename_plan_reports_passthrough(self) -> None:
        backend = FakePassthroughBackend()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            plan = service.vm_rename_plan(
                "vm1", "vm2", rename_disks=False
            )
        self.assertTrue(
            any("passthrough" in reason for reason in plan["reasons"])
        )


class DomainDictTests(unittest.TestCase):
    def test_passthrough_fields_are_serialized(self) -> None:
        payload = service._domain_dict(
            DomainInfo(
                "vm1",
                "uuid",
                "running",
                ["/tmp/disk.qcow2"],
                False,
                "bios",
                "External",
                passthrough=True,
                passthrough_detail="pci x2",
            )
        )
        self.assertTrue(payload["passthrough"])
        self.assertEqual(payload["passthrough_detail"], "pci x2")


class DoctorPassthroughTests(unittest.TestCase):
    def test_warning_lists_passthrough_domains(self) -> None:
        check = diagnose._passthrough_check(
            [
                DomainInfo(
                    "vm1",
                    "uuid",
                    "stopped",
                    [],
                    passthrough=True,
                    passthrough_detail="pci x1",
                ),
                DomainInfo("vm2", "uuid2", "stopped", []),
            ]
        )
        self.assertEqual(check["check"], "host_passthrough")
        self.assertEqual(check["level"], "warn")
        self.assertFalse(check["ok"])
        self.assertIn("vm1 (pci x1)", check["detail"])
        self.assertEqual(check["domains"], ["vm1"])

    def test_no_passthrough_is_ok(self) -> None:
        check = diagnose._passthrough_check(
            [DomainInfo("vm1", "uuid", "stopped", [])]
        )
        self.assertEqual(check["level"], "ok")
        self.assertTrue(check["ok"])


if __name__ == "__main__":
    unittest.main()
