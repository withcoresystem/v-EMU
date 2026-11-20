from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vemu import service
from vemu.backend import DomainInfo


class FakeBackend:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def info(self, name: str):
        return DomainInfo(
            name,
            "uuid",
            "stopped",
            [],
            False,
            "bios",
            "v-EMU (full)",
        )

    def undefine(self, name: str, force: bool = False) -> None:
        return None


class DomainDeleteHostCleanupTests(unittest.TestCase):
    def _write_state(self, root: Path) -> dict:
        state_dir = root / "vemu" / "seamless"
        state_dir.mkdir(parents=True)
        shortcut_dir = root / "applications"
        shortcut_dir.mkdir()
        key = state_dir / "vm1.id_ed25519"
        known = state_dir / "vm1.known_hosts"
        key.write_text("private")
        known.write_text("known")
        (state_dir / "vm1.json").write_text(
            json.dumps(
                {
                    "domain": "vm1",
                    "active": True,
                    "username": "ProUser",
                    "apps": [
                        {"name": "Notepad", "path": "C:\\notepad.exe"}
                    ],
                    "key_path": str(key),
                    "known_hosts": str(known),
                }
            )
        )
        (shortcut_dir / "vemu-seamless-vm1-Notepad.desktop").write_text(
            "[Desktop Entry]\n"
        )
        icon_dir = shortcut_dir.parent / "vemu" / "seamless" / "icons"
        icon_dir.mkdir(parents=True)
        (icon_dir / "vemu-seamless-vm1-Notepad.png").write_bytes(b"png")

        experimental = root / "experimental"
        experimental.mkdir()
        xml = experimental / "vm1.domain.xml"
        xml.write_text("<domain/>")
        exp_key = experimental / "vm1.id_ed25519"
        exp_key.write_text("private")
        (experimental / "vm1.json").write_text(
            json.dumps(
                {
                    "domain": "vm1",
                    "enabled": True,
                    "xml_backup": str(xml),
                    "host_changes": {
                        "backup": "/etc/libvirt/qemu.conf.vemu-3d-backup"
                    },
                }
            )
        )
        return {
            "state_dir": state_dir,
            "shortcut_dir": shortcut_dir,
            "key": key,
            "known": known,
            "icon": icon_dir / "vemu-seamless-vm1-Notepad.png",
            "xml": xml,
            "exp_key": exp_key,
            "experimental": experimental,
        }

    def test_delete_vm_only_keeps_state_but_removes_shortcuts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._write_state(root)
            with mock.patch.dict(
                os.environ,
                {
                    "VEMU_EXPERIMENTAL_STATE_DIR": str(paths["experimental"]),
                    "VEMU_BACKUP_DIR": str(root / "backups"),
                    "VEMU_BASELINE_DIR": str(root / "baselines"),
                    "VEMU_SHARE_CONFIG": str(root / "shares.json"),
                },
            ), mock.patch(
                "vemu.service.LibvirtBackend", return_value=FakeBackend()
            ), mock.patch(
                "vemu.service.credentials.delete_password"
            ) as forget:
                result = service.domain_delete(
                    "vm1",
                    delete_storage=False,
                    state_dir=paths["state_dir"],
                    shortcut_dir=paths["shortcut_dir"],
                )
            self.assertTrue(paths["key"].is_file())
            self.assertTrue(paths["known"].is_file())
            self.assertTrue((paths["experimental"] / "vm1.json").is_file())
            self.assertFalse(paths["icon"].exists())
            self.assertEqual(
                list(paths["shortcut_dir"].glob("*.desktop")), []
            )
            self.assertTrue(
                (paths["state_dir"] / "vm1.json").exists()
            )
            self.assertTrue(
                any(
                    "qemu.conf" in item
                    for item in result["host_cleanup"]["warnings"]
                )
            )
            forget.assert_not_called()

    def test_delete_vm_and_disks_purges_host_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = self._write_state(root)
            with mock.patch.dict(
                os.environ,
                {
                    "VEMU_EXPERIMENTAL_STATE_DIR": str(paths["experimental"]),
                    "VEMU_BACKUP_DIR": str(root / "backups"),
                    "VEMU_BASELINE_DIR": str(root / "baselines"),
                    "VEMU_SHARE_CONFIG": str(root / "shares.json"),
                },
            ), mock.patch(
                "vemu.service.LibvirtBackend", return_value=FakeBackend()
            ), mock.patch(
                "vemu.service.credentials.delete_password"
            ) as forget:
                result = service.domain_delete(
                    "vm1",
                    delete_storage=True,
                    state_dir=paths["state_dir"],
                    shortcut_dir=paths["shortcut_dir"],
                )
            self.assertFalse((paths["state_dir"] / "vm1.json").exists())
            self.assertFalse(paths["key"].exists())
            self.assertFalse(paths["known"].exists())
            self.assertFalse(paths["xml"].exists())
            self.assertFalse(
                (paths["experimental"] / "vm1.json").exists()
            )
            self.assertFalse(paths["exp_key"].exists())
            self.assertFalse(paths["icon"].exists())
            forget.assert_called_once()
            self.assertTrue(
                any(
                    "qemu.conf" in item
                    for item in result["host_cleanup"]["warnings"]
                )
            )


if __name__ == "__main__":
    unittest.main()
