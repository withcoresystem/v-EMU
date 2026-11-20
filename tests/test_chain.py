from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from vemu.chain import ChainManager
from vemu.errors import Conflict, ValidationError

QEMU_IMG = shutil.which("qemu-img")
QEMU_IO = shutil.which("qemu-io")


@unittest.skipUnless(QEMU_IMG, "qemu-img is required")
class ChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.base = self.root / "base.qcow2"
        subprocess.run(
            [QEMU_IMG, "create", "-f", "qcow2", str(self.base), "16M"],
            check=True,
            capture_output=True,
        )
        self.chain_dir = self.root / "chain"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _create(self) -> ChainManager:
        return ChainManager.create(self.chain_dir, self.base)

    def _create_legacy(self) -> ChainManager:
        return ChainManager.create(
            self.chain_dir, self.base, snapshot_model=1
        )

    def test_create(self) -> None:
        manager = self._create()
        chain = manager.load()
        self.assertEqual(chain.active, "0001")
        self.assertEqual(len(chain.layers), 1)
        self.assertEqual(chain.version, 2)
        self.assertIn("vda", chain.disks)
        self.assertTrue(Path(chain.active_disk()).is_file())
        self.assertTrue(manager.chain_file.is_file())
        self.assertEqual(manager.verify(), [])

    def test_new_chain_uses_snapshot_model_two(self) -> None:
        manager = self._create()
        self.assertEqual(manager.load().snapshot_model, 2)
        data = json.loads(manager.chain_file.read_text(encoding="utf-8"))
        self.assertEqual(data["snapshot_model"], 2)

    def test_snapshot_freezes_current_and_starts_a_new_layer(self) -> None:
        manager = self._create()
        snapshot = manager.snapshot("before-change")
        self.assertEqual(snapshot.id, "0001")
        self.assertEqual(snapshot.label, "before-change")
        chain = manager.load()
        self.assertEqual(chain.active, "0002")
        self.assertEqual(chain.layer("0002").label, "")
        self.assertEqual(chain.layer("0002").parent, "0001")
        self.assertEqual(chain.layer("0001").label, "before-change")
        self.assertEqual(manager.verify(), [])

    def test_snapshot_requires_a_name(self) -> None:
        manager = self._create()
        with self.assertRaises(ValidationError):
            manager.snapshot("  ")

    def test_revert_discards_current_and_starts_a_new_layer(self) -> None:
        manager = self._create()
        manager.snapshot("before-change")
        discarded = Path(manager.load().active_disk())
        manager.revert("0001")
        chain = manager.load()
        self.assertEqual(chain.active, "0003")
        self.assertEqual(
            [layer.id for layer in chain.layers], ["0001", "0003"]
        )
        self.assertEqual(chain.layer("0001").label, "before-change")
        self.assertEqual(chain.layer("0003").parent, "0001")
        self.assertFalse(discarded.exists())
        self.assertEqual(manager.verify(), [])

    @unittest.skipUnless(QEMU_IO, "qemu-io is required")
    def test_revert_discards_writes_from_the_current_layer(self) -> None:
        manager = self._create()
        manager.snapshot("clean")
        active = Path(manager.load().active_disk())
        subprocess.run(
            [QEMU_IO, "-f", "qcow2", "-c", "write -P 0xAB 0 1M", str(active)],
            check=True,
            capture_output=True,
        )
        manager.revert("0001")
        new_active = Path(manager.load().active_disk())
        self.assertNotEqual(active, new_active)
        self.assertFalse(active.exists())
        probe = subprocess.run(
            [QEMU_IO, "-f", "qcow2", "-c", "read -P 0xAB 0 512", str(new_active)],
            capture_output=True,
            text=True,
        )
        self.assertIn("Pattern verification failed", probe.stdout)

    def test_snapshot_description_and_update(self) -> None:
        manager = self._create()
        layer = manager.snapshot(
            "before-change", "Clean state before the upgrade"
        )
        self.assertEqual(layer.id, "0001")
        self.assertEqual(layer.description, "Clean state before the upgrade")
        info = manager.info()
        frozen = next(
            item for item in info["layers"] if item["id"] == layer.id
        )
        self.assertEqual(
            frozen["description"], "Clean state before the upgrade"
        )

        updated = manager.update_snapshot(
            layer.id,
            label="before-upgrade",
            description="Retry after the network fix",
        )
        self.assertEqual(updated.label, "before-upgrade")
        self.assertEqual(updated.description, "Retry after the network fix")
        self.assertEqual(manager.load().active, "0002")
        with self.assertRaises(Conflict):
            manager.update_snapshot("0002", label="not-a-snapshot")
        self.assertEqual(manager.verify(), [])

    def test_snapshot_branch_after_revert(self) -> None:
        manager = self._create()
        manager.snapshot("first-branch")
        manager.snapshot("second-branch")
        manager.revert("0001")
        chain = manager.load()
        self.assertEqual(chain.active, "0004")
        self.assertEqual(
            [layer.id for layer in chain.children("0001")],
            ["0002", "0004"],
        )
        self.assertEqual(manager.verify(), [])

        manager.revert("0002")
        chain = manager.load()
        self.assertEqual(chain.active, "0005")
        self.assertEqual(
            [layer.id for layer in chain.children("0001")], ["0002"]
        )
        manager.delete("0002")
        chain = manager.load()
        self.assertEqual(
            [layer.id for layer in chain.layers], ["0001", "0005"]
        )
        self.assertEqual(chain.layer("0005").parent, "0001")
        self.assertEqual(manager.verify(), [])

    def test_delete_branched_parent_requires_leaf_cleanup(self) -> None:
        manager = self._create()
        manager.snapshot("first-branch")
        manager.snapshot("second-branch")
        manager.revert("0001")
        with self.assertRaises(Conflict) as raised:
            manager.delete("0001")
        self.assertIn("branched", str(raised.exception))

    def test_delete_middle(self) -> None:
        manager = self._create()
        manager.snapshot("b1")
        manager.snapshot("b2")
        self.assertEqual([layer.id for layer in manager.load().layers], ["0001", "0002", "0003"])

        manager.delete("0002")
        chain = manager.load()
        self.assertEqual([layer.id for layer in chain.layers], ["0001", "0003"])
        self.assertEqual(chain.layer("0003").parent, "0001")
        self.assertEqual(chain.active, "0003")
        self.assertTrue(Path(chain.layer("0003").path).is_file())
        self.assertFalse((self.chain_dir / "disk-vda-0002.qcow2").exists())
        self.assertEqual(manager.verify(), [])

    def test_delete_first_with_children_fails(self) -> None:
        manager = self._create()
        manager.snapshot("b1")
        with self.assertRaises(Conflict):
            manager.delete("0001")

    def test_delete_only_layer_fails(self) -> None:
        manager = self._create()
        with self.assertRaises(Conflict):
            manager.delete("0001")

    def test_delete_current_layer_is_blocked(self) -> None:
        manager = self._create()
        manager.snapshot("b1")
        with self.assertRaises(Conflict):
            manager.delete("0002")
        with self.assertRaises(Conflict):
            manager.delete("0002", force=True)
        chain = manager.load()
        self.assertEqual(chain.active, "0002")
        self.assertEqual(len(chain.layers), 2)

    def test_legacy_snapshot_model_keeps_old_semantics(self) -> None:
        manager = self._create_legacy()
        self.assertEqual(manager.load().snapshot_model, 1)
        layer = manager.snapshot("before-change")
        self.assertEqual(layer.id, "0002")
        self.assertEqual(manager.load().active, "0002")
        manager.revert("0001")
        self.assertEqual(manager.load().active, "0001")
        manager.delete("0002")
        self.assertEqual(manager.load().active, "0001")

    @unittest.skipUnless(QEMU_IO, "qemu-io is required")
    def test_compact_flattens_current_state_and_reclaims_layers(self) -> None:
        manager = self._create()
        manager.snapshot("before-change")
        active = Path(manager.load().active_disk())
        subprocess.run(
            [QEMU_IO, "-f", "qcow2", "-c", "write -P 0xCD 0 1M", str(active)],
            check=True,
            capture_output=True,
        )
        plan = manager.compact_plan()
        self.assertTrue(plan["supported"])
        self.assertGreaterEqual(plan["snapshot_count"], 1)
        self.assertTrue(plan["requires_snapshot_loss"])
        with self.assertRaises(ValidationError):
            manager.compact()
        result = manager.compact(confirm_snapshot_loss=True)
        self.assertTrue(result["compacted"])
        chain = manager.load()
        self.assertEqual(chain.snapshot_model, 2)
        self.assertEqual(len(chain.layers), 1)
        self.assertEqual(chain.active, "0001")
        self.assertEqual(Path(chain.base), self.base)
        self.assertFalse(active.exists())
        self.assertTrue(Path(chain.active_disk()).is_file())
        self.assertTrue(Path(chain.base).is_file())
        probe = subprocess.run(
            [
                QEMU_IO,
                "-f",
                "qcow2",
                "-c",
                "read -P 0xCD 0 512",
                chain.active_disk(),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotIn("Pattern verification failed", probe.stdout)
        self.assertEqual(manager.verify(), [])

    @unittest.skipUnless(QEMU_IO, "qemu-io is required")
    def test_compact_legacy_chain_converts_to_model_two(self) -> None:
        manager = self._create_legacy()
        manager.snapshot("legacy-state")
        active = Path(manager.load().active_disk())
        subprocess.run(
            [QEMU_IO, "-f", "qcow2", "-c", "write -P 0xEF 0 1M", str(active)],
            check=True,
            capture_output=True,
        )
        manager.compact(confirm_snapshot_loss=True)
        chain = manager.load()
        self.assertEqual(chain.snapshot_model, 2)
        self.assertEqual(len(chain.layers), 1)
        self.assertEqual(chain.active, "0001")
        self.assertFalse(active.exists())
        probe = subprocess.run(
            [
                QEMU_IO,
                "-f",
                "qcow2",
                "-c",
                "read -P 0xEF 0 512",
                chain.active_disk(),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotIn("Pattern verification failed", probe.stdout)
        self.assertEqual(manager.verify(), [])

    def test_compact_restores_chain_when_domain_update_fails(self) -> None:
        manager = self._create()
        manager.snapshot("before-change")
        before = manager.load()
        old_active = Path(before.active_disk())

        def fail(_new_chain) -> None:
            raise RuntimeError("domain update failed")

        with self.assertRaises(RuntimeError):
            manager.compact(
                confirm_snapshot_loss=True,
                update_domain=fail,
            )
        after = manager.load()
        self.assertEqual(after.active, before.active)
        self.assertEqual(len(after.layers), 2)
        self.assertTrue(old_active.exists())
        self.assertEqual(manager.verify(), [])

    def test_compact_plan_blocks_missing_base(self) -> None:
        manager = self._create()
        manager.snapshot("before-change")
        self.base.unlink()
        plan = manager.compact_plan()
        self.assertFalse(plan["supported"])
        self.assertTrue(
            any("Base image" in reason for reason in plan["reasons"])
        )

    def test_compact_plan_blocks_single_layer(self) -> None:
        manager = self._create()
        plan = manager.compact_plan()
        self.assertFalse(plan["supported"])
        self.assertTrue(
            any("one layer" in reason for reason in plan["reasons"])
        )
        with self.assertRaises(Conflict):
            manager.compact(confirm_snapshot_loss=True)

    @unittest.skipUnless(QEMU_IO, "qemu-io is required")
    def test_compact_reports_nonnegative_reclaim_after_merging_data(self) -> None:
        manager = self._create()
        manager.snapshot("S1")
        first_child = Path(manager.load().active_disk())
        subprocess.run(
            [QEMU_IO, "-f", "qcow2", "-c", "write -P 0x11 0 1M", str(first_child)],
            check=True,
            capture_output=True,
        )
        manager.snapshot("S2")
        active = Path(manager.load().active_disk())
        subprocess.run(
            [QEMU_IO, "-f", "qcow2", "-c", "write -P 0x22 1M 1M", str(active)],
            check=True,
            capture_output=True,
        )
        result = manager.compact(confirm_snapshot_loss=True)
        self.assertGreater(result["removed_bytes"], 0)
        self.assertGreaterEqual(result["reclaim_bytes"], 0)
        self.assertIsInstance(result["host_reclaim_bytes"], int)
        self.assertGreater(result["first_bytes"], 0)
        self.assertEqual(manager.verify(), [])

    @unittest.skipUnless(QEMU_IO, "qemu-io is required")
    def test_compact_commit_discard_shrinks_first_layer(self) -> None:
        manager = self._create()
        first = Path(manager.load().active_disk())
        subprocess.run(
            [QEMU_IO, "-f", "qcow2", "-c", "write -P 0x55 0 4M", str(first)],
            check=True,
            capture_output=True,
        )
        manager.snapshot("S1")
        current = Path(manager.load().active_disk())
        subprocess.run(
            [QEMU_IO, "-f", "qcow2", "-c", "discard 0 4M", str(current)],
            check=True,
            capture_output=True,
        )
        result = manager.compact(confirm_snapshot_loss=True)
        self.assertLess(result["first_bytes"], 4 * 1024 * 1024)
        self.assertEqual(manager.load().active, "0001")
        self.assertEqual(manager.verify(), [])

    @unittest.skipUnless(QEMU_IO, "qemu-io is required")
    def test_compact_keeps_active_branch_and_discards_side_branch(self) -> None:
        manager = self._create()
        manager.snapshot("S1")
        branch = Path(manager.load().active_disk())
        subprocess.run(
            [QEMU_IO, "-f", "qcow2", "-c", "write -P 0xAA 0 1M", str(branch)],
            check=True,
            capture_output=True,
        )
        manager.snapshot("S2")
        manager.revert("0001")
        active = Path(manager.load().active_disk())
        subprocess.run(
            [QEMU_IO, "-f", "qcow2", "-c", "write -P 0xBB 1M 1M", str(active)],
            check=True,
            capture_output=True,
        )
        plan = manager.compact_plan()
        self.assertTrue(plan["supported"])
        self.assertGreaterEqual(plan["branch_count"], 1)
        manager.compact(confirm_snapshot_loss=True)
        chain = manager.load()
        self.assertEqual(chain.active, "0001")
        self.assertEqual(len(chain.layers), 1)
        self.assertFalse(branch.exists())
        self.assertFalse(active.exists())
        keep = subprocess.run(
            [
                QEMU_IO,
                "-f",
                "qcow2",
                "-c",
                "read -P 0xBB 1M 512",
                chain.active_disk(),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotIn("Pattern verification failed", keep.stdout)
        dropped = subprocess.run(
            [
                QEMU_IO,
                "-f",
                "qcow2",
                "-c",
                "read -P 0xAA 0 512",
                chain.active_disk(),
            ],
            capture_output=True,
            text=True,
        )
        self.assertIn("Pattern verification failed", dropped.stdout)
        self.assertEqual(manager.verify(), [])

    def test_info_reports_sizes(self) -> None:
        manager = self._create()
        manager.snapshot("b1")
        info = manager.info()
        self.assertEqual(info["active"], "0002")
        self.assertEqual(len(info["layers"]), 2)
        self.assertGreaterEqual(info["layers"][0]["actual_size"], 0)

    def test_chain_backup_written(self) -> None:
        manager = self._create()
        manager.snapshot("b1")
        backup = manager.chain_file.with_suffix(".json.bak")
        self.assertTrue(backup.is_file())

    def test_find_orphan_layers(self) -> None:
        from vemu.chain import find_orphan_layers

        manager = self._create()
        manager.snapshot("b1")
        orphan = self.chain_dir / "disk-9999.qcow2"
        orphan.write_bytes(b"orphan")
        template = self.root / "template.qcow2"
        template.write_bytes(b"template")
        self.assertEqual(find_orphan_layers(self.root), [orphan])

    def test_recover_interrupted_commit(self) -> None:
        from unittest import mock

        from vemu.chain import COMMIT_JOURNAL, find_commit_journals

        manager = self._create()
        manager.snapshot("b1")
        manager.snapshot("b2")
        with mock.patch.object(
            manager, "_run", side_effect=RuntimeError("qemu killed")
        ):
            with self.assertRaises(RuntimeError):
                manager.delete("0002")
        journal = self.chain_dir / COMMIT_JOURNAL
        self.assertTrue(journal.is_file())
        self.assertIn("commit journal", "\n".join(manager.verify()))
        self.assertEqual(find_commit_journals(self.root), [journal])

        result = manager.recover_commit()
        self.assertIsNotNone(result)
        self.assertTrue(result["recovered"])
        chain = manager.load()
        self.assertEqual([layer.id for layer in chain.layers], ["0001", "0003"])
        self.assertEqual(chain.layer("0003").parent, "0001")
        self.assertEqual(chain.active, "0003")
        self.assertFalse(journal.exists())
        self.assertEqual(manager.verify(), [])


if __name__ == "__main__":
    unittest.main()
