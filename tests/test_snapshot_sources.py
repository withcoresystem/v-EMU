from __future__ import annotations

import unittest
import xml.etree.ElementTree as ET

from vemu.backend import LibvirtBackend
from vemu.errors import NotFound

DISK_XML = """<domain type='kvm'>
  <devices>
    <disk type='file' device='disk'>
      <source file='/labs/vm/disk-0010.qcow2'/>
      <target dev='vda' bus='virtio'/>
      <backingStore type='file'>
        <format type='qcow2'/>
        <source file='/labs/vm/disk-0001.qcow2'/>
        <backingStore type='file'>
          <format type='qcow2'/>
          <source file='/labs/vm/base.qcow2'/>
        </backingStore>
      </backingStore>
    </disk>
    <disk type='file' device='disk'>
      <source file='/labs/vm/data.qcow2'/>
      <target dev='vdb' bus='virtio'/>
    </disk>
  </devices>
</domain>"""


class FakeDomain:
    def __init__(self, xml: str) -> None:
        self._xml = xml

    def state(self):
        return [5]  # shut off

    def XMLDesc(self, _flags: int = 0) -> str:
        return self._xml


class FakeBackend(LibvirtBackend):
    def __init__(self, xml: str) -> None:
        self._xml = xml
        self.defined: str | None = None

    def domain(self, _name: str) -> FakeDomain:
        return FakeDomain(self._xml)

    def redefine_domain(self, _name: str, xml: str) -> str:
        self.defined = xml
        return xml


class SnapshotSourceUpdateTests(unittest.TestCase):
    def test_source_change_drops_stale_backing_store(self) -> None:
        backend = FakeBackend(DISK_XML)
        result = backend.update_disk_source(
            "vm",
            "/labs/vm/disk-0011.qcow2",
            old_path="/labs/vm/disk-0010.qcow2",
        )
        self.assertIsInstance(result, str)
        root = ET.fromstring(result)
        disks = root.findall("./devices/disk")
        self.assertEqual(
            disks[0].find("source").get("file"),
            "/labs/vm/disk-0011.qcow2",
        )
        self.assertIsNone(disks[0].find("backingStore"))
        # An unrelated disk keeps its own definition.
        self.assertEqual(
            disks[1].find("source").get("file"),
            "/labs/vm/data.qcow2",
        )

    def test_bulk_source_change_drops_backing_store(self) -> None:
        xml = DISK_XML.replace(
            "<disk type='file' device='disk'>\n"
            "      <source file='/labs/vm/data.qcow2'/>",
            "<disk type='file' device='disk'>\n"
            "      <source file='/labs/vm/data-old.qcow2'/>\n"
            "      <backingStore type='file'><source file='/labs/vm/base.qcow2'/></backingStore>",
        )
        backend = FakeBackend(xml)
        result = backend.update_disk_sources(
            "vm",
            {
                "/labs/vm/disk-0010.qcow2": "/labs/vm/disk-0011.qcow2",
                "/labs/vm/data-old.qcow2": "/labs/vm/data.qcow2",
            },
        )
        root = ET.fromstring(result)
        for disk in root.findall("./devices/disk"):
            self.assertIsNone(disk.find("backingStore"))
        self.assertEqual(
            root.findall("./devices/disk")[1]
            .find("source")
            .get("file"),
            "/labs/vm/data.qcow2",
        )

    def test_missing_source_is_reported(self) -> None:
        backend = FakeBackend(DISK_XML)
        with self.assertRaises(NotFound):
            backend.update_disk_source(
                "vm",
                "/labs/vm/other.qcow2",
                old_path="/labs/vm/missing.qcow2",
            )


if __name__ == "__main__":
    unittest.main()
