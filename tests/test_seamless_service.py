from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vemu import service
from vemu.errors import Conflict
from vemu.seamless.prepare import SeamlessPrepare

STATE = {
    "branch": "seamless",
    "domain": "vm1",
    "host": "192.0.2.10",
    "username": "ProUser",
    "profile": "Safe",
    "apps": [{"name": "Notepad", "path": "C:\\notepad.exe"}],
    "key_path": "/tmp/key",
    "known_hosts": "/tmp/known_hosts",
}


class FakeBackend:
    def __init__(self, state: str = "running") -> None:
        self.state = state
        self.commands: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def info(self, name: str):
        return SimpleNamespace(name=name, state=self.state)

    def config_xml(self, _name: str) -> str:
        return (
            "<domain><devices><video><model type='qxl'/>"
            "</video></devices></domain>"
        )

    def shutdown(self, _name, timeout=90, force=False):
        self.state = "stopped"

    def start(self, _name):
        self.state = "running"
        return []

    def redefine_domain(self, _name, _xml):
        return None

    def qemu_agent_command(self, domain, command, *, timeout=30):
        self.commands.append(command)
        if command.get("execute") == "guest-ping":
            return {}
        if command.get("execute") == "guest-network-get-interfaces":
            return [
                {
                    "hardware-address": "52:54:00:00:00:01",
                    "ip-addresses": [
                        {
                            "ip-address-type": "ipv4",
                            "ip-address": "192.0.2.10",
                        }
                    ],
                }
            ]
        raise AssertionError(command)


class SeamlessServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        status = SimpleNamespace(
            level="ok",
            message="FreeRDP 3.33.1",
            as_dict=lambda: {
                "binary": "/usr/bin/xfreerdp",
                "version": "3.33.1",
                "level": "ok",
                "message": "FreeRDP 3.33.1",
            },
        )
        for name in ("ensure_freerdp_compatible", "freerdp_status"):
            patcher = mock.patch(
                f"vemu.service.{name}", return_value=status
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def patch_state_dir(self, tmp: str) -> None:
        patcher = mock.patch.dict(
            "os.environ", {"XDG_STATE_HOME": tmp}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prepare_builds_plan(self) -> None:
        backend = FakeBackend()
        captured: dict = {}

        def fake_prepare(transport, plan):
            captured["plan"] = plan
            return dict(STATE)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ), mock.patch(
                "vemu.service.prepare_seamless", side_effect=fake_prepare
            ), mock.patch(
                "vemu.service.seamless_icons.extract", return_value=None
            ):
                result = service.vm_seamless_prepare(
                    "vm1",
                    "ProUser",
                    profile="Safe",
                    apps={"Notepad": "C:\\notepad.exe"},
                    state_dir=Path(tmp) / "vemu" / "seamless",
                    shortcut_dir=Path(tmp),
                    snapshot=False,
                )
            self.assertTrue(
                (Path(tmp) / "vemu-seamless-vm1-Notepad.desktop").is_file()
            )
        plan = captured["plan"]
        self.assertIsInstance(plan, SeamlessPrepare)
        self.assertEqual(plan.host, "192.0.2.10")
        self.assertEqual(plan.username, "ProUser")
        self.assertEqual(plan.domain, "vm1")
        self.assertEqual(plan.apps, {"Notepad": "C:\\notepad.exe"})
        self.assertEqual(result["domain"], "vm1")

    def test_prepare_reuses_last_apps_after_deactivate(self) -> None:
        backend = FakeBackend()
        captured: dict = {}

        def fake_prepare(transport, plan):
            captured["plan"] = plan
            return dict(STATE)

        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp) / "vemu" / "seamless"
            state_dir.mkdir(parents=True)
            saved = dict(STATE)
            saved["active"] = False
            saved["apps"] = []
            saved["last_apps"] = [
                {"name": "Notepad", "path": "C:\\notepad.exe"}
            ]
            (state_dir / "vm1.json").write_text(json.dumps(saved))
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ), mock.patch(
                "vemu.service.prepare_seamless", side_effect=fake_prepare
            ), mock.patch(
                "vemu.service.seamless_icons.extract", return_value=None
            ):
                service.vm_seamless_prepare(
                    "vm1",
                    "ProUser",
                    profile="Safe",
                    apps=None,
                    state_dir=state_dir,
                    shortcut_dir=Path(tmp),
                    snapshot=False,
                )
        self.assertEqual(
            captured["plan"].apps,
            {"Notepad": {"name": "Notepad", "path": "C:\\notepad.exe"}},
        )

    def test_prepare_requires_running(self) -> None:
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeBackend("stopped")
        ):
            with self.assertRaises(Conflict):
                service.vm_seamless_prepare("vm1", "ProUser")

    def test_prepare_requires_guest_agent(self) -> None:
        backend = FakeBackend()

        def no_ping(domain, command, *, timeout=30):
            raise service.VemuError("agent down")

        backend.qemu_agent_command = no_ping
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            with self.assertRaises(Conflict):
                service.vm_seamless_prepare(
                    "vm1", "ProUser", snapshot=False
                )

    def test_prepare_requires_guest_ip(self) -> None:
        backend = FakeBackend()

        def no_ip(domain, command, *, timeout=30):
            if command.get("execute") == "guest-ping":
                return {}
            return []

        backend.qemu_agent_command = no_ip
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ):
            with self.assertRaises(Conflict) as caught:
                service.vm_seamless_prepare(
                    "vm1", "ProUser", snapshot=False
                )
        self.assertIn("No IPv4", str(caught.exception))

    def test_prepare_uses_explicit_ip(self) -> None:
        backend = FakeBackend()
        captured: dict = {}

        def fake_prepare(transport, plan):
            captured["plan"] = plan
            return dict(STATE)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ), mock.patch(
                "vemu.service.prepare_seamless", side_effect=fake_prepare
            ), mock.patch(
                "vemu.service.seamless_icons.extract", return_value=None
            ):
                service.vm_seamless_prepare(
                    "vm1",
                    "ProUser",
                    guest_ip="10.0.0.5",
                    state_dir=Path(tmp) / "vemu" / "seamless",
                    shortcut_dir=Path(tmp),
                    snapshot=False,
                )
        self.assertEqual(captured["plan"].host, "10.0.0.5")

    def test_status_reads_saved_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_state_dir(tmp)
            state_file = Path(tmp) / "vemu" / "seamless" / "vm1.json"
            state_file.parent.mkdir(parents=True)
            state_file.write_text(json.dumps(STATE))
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=FakeBackend()
            ), mock.patch(
                "vemu.service.seamless_preflight",
                return_value={"supported": True, "reasons": []},
            ):
                result = service.vm_seamless_status("vm1")
        self.assertTrue(result["prepared"])
        self.assertTrue(result["running"])
        self.assertTrue(result["preflight"]["supported"])
        self.assertEqual(result["state"]["username"], "ProUser")

    def test_status_without_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_state_dir(tmp)
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=FakeBackend()
            ), mock.patch(
                "vemu.service.seamless_preflight",
                return_value={"supported": True, "reasons": []},
            ):
                result = service.vm_seamless_status("vm1")
        self.assertFalse(result["prepared"])
        self.assertIsNone(result["state"])

    def test_status_reports_preflight_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_state_dir(tmp)
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=FakeBackend()
            ), mock.patch(
                "vemu.service.seamless_preflight",
                side_effect=service.VemuError("bad guest"),
            ):
                result = service.vm_seamless_status("vm1")
        self.assertEqual(result["preflight_error"], "bad guest")

    def test_apps_uses_saved_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_state_dir(tmp)
            state_file = Path(tmp) / "vemu" / "seamless" / "vm1.json"
            state_file.parent.mkdir(parents=True)
            state_file.write_text(json.dumps(STATE))
            transport = object()
            entry = SimpleNamespace(
                to_dict=lambda: {
                    "name": "Notepad",
                    "path": "C:\\notepad.exe",
                    "arguments": "",
                },
                software_gpu=False,
                launch_arguments="",
            )
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=FakeBackend()
            ), mock.patch(
                "vemu.service.SSHTransport", return_value=transport
            ) as ssh, mock.patch(
                "vemu.service.discover_start_menu_apps",
                return_value=[entry],
            ):
                result = service.vm_seamless_apps("vm1")
        ssh.assert_called_once_with(
            "192.0.2.10", "ProUser", Path("/tmp/key"), Path("/tmp/known_hosts")
        )
        self.assertEqual(result["apps"][0]["name"], "Notepad")

    def test_apps_requires_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_state_dir(tmp)
            with self.assertRaises(Conflict):
                service.vm_seamless_apps("vm1")

    def test_add_app(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.patch_state_dir(tmp)
            backend = FakeBackend()
            with mock.patch(
                "vemu.service.LibvirtBackend", return_value=backend
            ), mock.patch(
                "vemu.service.seamless_add_remoteapp_app",
                return_value={"Name": "Notepad"},
            ) as add, mock.patch(
                "vemu.service.seamless_icons.extract", return_value=None
            ):
                result = service.vm_seamless_add_app(
                    "vm1",
                    "Notepad",
                    "C:\\notepad.exe",
                    shortcut_dir=Path(tmp),
                )
            self.assertTrue(
                (Path(tmp) / "vemu-seamless-vm1-Notepad.desktop").is_file()
            )
        add.assert_called_once()
        self.assertEqual(result["app"]["Name"], "Notepad")


class SeamlessVideoTests(unittest.TestCase):
    def test_prepare_rejects_non_qxl(self) -> None:
        backend = FakeBackend()
        backend.config_xml = lambda _name: (
            "<domain><devices><video><model type='virtio'/>"
            "</video></devices></domain>"
        )
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ), mock.patch(
            "vemu.service.prepare_seamless"
        ) as prepare:
            with self.assertRaises(Conflict) as caught:
                service.vm_seamless_prepare(
                    "vm1", "ProUser", snapshot=False
                )
        self.assertIn("QXL/VGA", str(caught.exception))
        prepare.assert_not_called()

    def test_video_sets_qxl(self) -> None:
        with mock.patch(
            "vemu.service.vm_config_info",
            return_value={"config": {"video": "virtio"}},
        ), mock.patch(
            "vemu.service.vm_config_update",
            return_value={"pending_restart": True, "warnings": []},
        ) as update:
            result = service.vm_seamless_video("vm1", model="qxl")
        update.assert_called_once()
        self.assertTrue(result["changed"])
        self.assertTrue(result["restart_required"])
        self.assertEqual(result["video"], "qxl")

    def test_video_already_set(self) -> None:
        with mock.patch(
            "vemu.service.vm_config_info",
            return_value={"config": {"video": "qxl"}},
        ), mock.patch("vemu.service.vm_config_update") as update:
            result = service.vm_seamless_video("vm1")
        update.assert_not_called()
        self.assertFalse(result["changed"])

    def test_video_model_validated(self) -> None:
        with self.assertRaises(service.ValidationError):
            service.vm_seamless_video("vm1", model="virtio")


class SeamlessProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(
            "os.environ", {"XDG_STATE_HOME": self.temp.name}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        status = SimpleNamespace(
            level="ok",
            message="FreeRDP 3.33.1",
            as_dict=lambda: {
                "binary": "/usr/bin/xfreerdp",
                "version": "3.33.1",
                "level": "ok",
                "message": "FreeRDP 3.33.1",
            },
        )
        for name in ("ensure_freerdp_compatible", "freerdp_status"):
            freerdp = mock.patch(
                f"vemu.service.{name}", return_value=status
            )
            freerdp.start()
            self.addCleanup(freerdp.stop)
        self.state_file = (
            Path(self.temp.name) / "vemu" / "seamless" / "vm1.json"
        )
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text(json.dumps(STATE))

    def entry(self) -> object:
        from vemu.seamless.apps import AppEntry

        return AppEntry(
            "Notepad", "C:\\Windows\\notepad.exe", "", "C:\\Start\\Notepad.lnk"
        )

    def test_refresh_apps(self) -> None:
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeBackend()
        ), mock.patch(
            "vemu.service.SSHTransport", return_value=object()
        ), mock.patch(
            "vemu.service.discover_start_menu_apps",
            return_value=[self.entry()],
        ):
            result = service.vm_seamless_refresh_apps("vm1")
        self.assertEqual(result["available"][0]["name"], "Notepad")
        self.assertEqual(result["published"][0]["name"], "Notepad")
        self.assertEqual(result["new"], [])
        saved = json.loads(self.state_file.read_text())
        self.assertIn("last_refresh", saved)

    def test_refresh_does_not_mark_hidden_store_app_missing(self) -> None:
        saved = json.loads(self.state_file.read_text())
        saved["apps"].append(
            {
                "name": "Calculator",
                "path": "C:\\Windows\\explorer.exe",
                "arguments": (
                    "shell:AppsFolder\\"
                    "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
                ),
            }
        )
        self.state_file.write_text(json.dumps(saved))
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeBackend()
        ), mock.patch(
            "vemu.service.SSHTransport", return_value=object()
        ), mock.patch(
            "vemu.service.discover_start_menu_apps",
            return_value=[self.entry()],
        ):
            result = service.vm_seamless_refresh_apps(
                "vm1", include_store=False
            )
        self.assertEqual(
            [item["name"] for item in result["missing"]], []
        )

    def test_state_defaults_to_active(self) -> None:
        self.assertTrue(service.vm_seamless_state("vm1")["active"])

    def test_deactivate_clears_published_programs(self) -> None:
        result = service.vm_seamless_deactivate("vm1")
        self.assertFalse(result["active"])
        self.assertEqual(result["removed_apps"], ["Notepad"])
        saved = json.loads(self.state_file.read_text())
        self.assertFalse(saved["active"])
        self.assertEqual(saved["apps"], [])
        self.assertEqual(saved["last_apps"][0]["name"], "Notepad")
        self.assertFalse(service.vm_seamless_state("vm1")["active"])

    def test_deactivate_without_state(self) -> None:
        with self.assertRaises(Conflict):
            service.vm_seamless_deactivate("missing")

    def test_remove_app_updates_state_and_guest(self) -> None:
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=FakeBackend()
        ), mock.patch(
            "vemu.service.seamless_remove_remoteapp_app",
            return_value={"removed": True},
        ) as remove:
            result = service.vm_seamless_remove_app(
                "vm1", "Notepad", shortcut_dir=Path(self.temp.name)
            )
        remove.assert_called_once()
        self.assertTrue(result["allowlist_removed"])
        self.assertTrue(result["state_removed"])
        saved = json.loads(self.state_file.read_text())
        self.assertEqual(saved["apps"], [])

    def test_test_app(self) -> None:
        from vemu.seamless.launch import RailTest

        state = dict(STATE)
        state["rdp_cert_sha256"] = "A" * 64
        self.state_file.write_text(json.dumps(state))
        with mock.patch.dict("os.environ", {"DISPLAY": ":0"}), mock.patch(
            "vemu.service.vm_seamless_refresh_host",
            return_value={"changed": False},
        ), mock.patch(
            "vemu.service.test_rail_app",
            return_value=RailTest(
                ok=True,
                connected=True,
                reason="ok",
                log="",
                elapsed=8.0,
            ),
        ) as launch:
            result = service.vm_seamless_test_app(
                "vm1", "Notepad", "secret"
            )
        launch.assert_called_once()
        args = launch.call_args.args
        self.assertEqual(args[3], "C:\\notepad.exe")
        self.assertTrue(result["ok"])

    def test_test_app_requires_display(self) -> None:
        environment = {"XDG_STATE_HOME": self.temp.name}
        with mock.patch.dict("os.environ", environment, clear=True):
            with self.assertRaises(Conflict) as caught:
                service.vm_seamless_test_app("vm1", "Notepad", "secret")
        self.assertIn("desktop session", str(caught.exception))

class SeamlessRollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_root = Path(self.temp.name) / "vemu" / "seamless"
        self.state_root.mkdir(parents=True)
        patcher = mock.patch.dict(
            "os.environ", {"XDG_STATE_HOME": self.temp.name}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.state_file = self.state_root / "vm1.json"
        self.state_file.write_text(json.dumps(STATE))

    def manifest(self) -> dict:
        return {
            "version": 1,
            "domain": "vm1",
            "stage": "pre-prepare",
            "layer": "0004",
            "label": "seamless-prepare",
            "xml_backup": str(self.state_root / "vm1.domain.xml"),
            "xml_sha256": "0" * 64,
        }

    def test_rollback_layer_is_protected(self) -> None:
        service._write_rollback_manifest("vm1", self.manifest())
        found = service._seamless_rollback_for_layer("vm1", "0004")
        self.assertIsNotNone(found)
        self.assertIsNone(
            service._seamless_rollback_for_layer("vm1", "0005")
        )

    def test_host_only_cleans_state_shortcut_and_icon(self) -> None:
        shortcut = Path(self.temp.name) / "vemu-seamless-vm1-Notepad.desktop"
        shortcut.write_text("x")
        icon = (
            Path(self.temp.name).parent
            / "vemu"
            / "seamless"
            / "icons"
            / "vemu-seamless-vm1-Notepad.png"
        )
        icon.parent.mkdir(parents=True, exist_ok=True)
        icon.write_bytes(b"x")
        result = service.vm_seamless_rollback(
            "vm1",
            host_only=True,
            shortcut_dir=Path(self.temp.name),
        )
        self.assertTrue(result["host_only"])
        self.assertFalse(self.state_file.exists())
        self.assertFalse(shortcut.exists())
        self.assertFalse(icon.exists())

    def test_missing_snapshot_keeps_host_state(self) -> None:
        manifest = self.manifest()
        service._write_rollback_manifest("vm1", manifest)
        xml = self.state_root / "vm1.domain.xml"
        xml.write_text("<domain/>")
        manifest["xml_sha256"] = service.hashlib.sha256(
            b"<domain/>"
        ).hexdigest()
        service._write_rollback_manifest("vm1", manifest)
        backend = FakeBackend("stopped")
        backend.redefine_domain = lambda *_args, **_kwargs: None
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ), mock.patch(
            "vemu.service.stop_seed", return_value=0
        ), mock.patch(
            "vemu.service.snapshots_revert",
            side_effect=service.NotFound("missing"),
        ):
            with self.assertRaises(service.Conflict):
                service.vm_seamless_rollback("vm1")
        self.assertTrue(self.state_file.exists())
        self.assertTrue(service._rollback_manifest_path("vm1").exists())

    def test_rollback_removes_host_artifacts(self) -> None:
        manifest = self.manifest()
        xml = self.state_root / "vm1.domain.xml"
        xml.write_text("<domain/>")
        manifest["xml_sha256"] = service.hashlib.sha256(
            b"<domain/>"
        ).hexdigest()
        service._write_rollback_manifest("vm1", manifest)
        backend = FakeBackend("stopped")
        backend.redefine_domain = lambda *_args, **_kwargs: None
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ), mock.patch(
            "vemu.service.stop_seed", return_value=0
        ), mock.patch(
            "vemu.service.snapshots_revert",
            return_value={},
        ):
            result = service.vm_seamless_rollback("vm1")
        self.assertTrue(result["reverted"])
        self.assertFalse(self.state_file.exists())
        self.assertFalse(service._rollback_manifest_path("vm1").exists())

    def test_ensure_creates_checkpoint_manifest(self) -> None:
        backend = FakeBackend("running")
        with mock.patch(
            "vemu.service.snapshots_create",
            return_value={
                "layer": {
                    "id": "0004",
                    "label": "seamless-prepare",
                    "created_at": "now",
                },
                "chain": str(self.state_root),
                "base": "/base.qcow2",
            },
        ) as create:
            manifest = service._ensure_seamless_rollback(
                "vm1",
                backend,
                state_dir=self.state_root,
                uri="qemu:///system",
            )
        self.assertEqual(manifest["layer"], "0004")
        create.assert_called_once()
        self.assertTrue(
            (self.state_root / "vm1.domain.xml").is_file()
        )
        self.assertTrue(
            service._rollback_manifest_path("vm1").is_file()
        )

    def test_prepare_records_rollback_manifest(self) -> None:
        backend = FakeBackend()
        captured = {}

        def fake_prepare(_transport, _plan):
            return dict(STATE)

        manifest = self.manifest()
        with mock.patch(
            "vemu.service.LibvirtBackend", return_value=backend
        ), mock.patch(
            "vemu.service._ensure_seamless_rollback",
            return_value=manifest,
        ), mock.patch(
            "vemu.service.prepare_seamless", side_effect=fake_prepare
        ), mock.patch(
            "vemu.service.seamless_icons.extract", return_value=None
        ):
            result = service.vm_seamless_prepare(
                "vm1",
                "ProUser",
                state_dir=self.state_root,
                shortcut_dir=Path(self.temp.name),
            )
        self.assertEqual(result["rollback"]["layer"], "0004")

    def test_ensure_reuses_orphan_checkpoint(self) -> None:
        backend = FakeBackend("running")
        info = {
            "base": "/base.qcow2",
            "active": "0003",
            "layers": [
                {
                    "id": "0002",
                    "label": "seamless-prepare",
                    "created_at": "2026-11-19T00:00:00+00:00",
                    "active": False,
                },
                {
                    "id": "0003",
                    "label": "",
                    "created_at": "2026-11-19T00:01:00+00:00",
                    "active": True,
                },
            ],
        }
        with mock.patch(
            "vemu.service._chain_dir_for_domain",
            return_value=Path(self.temp.name),
        ), mock.patch(
            "vemu.service.ChainManager"
        ) as manager, mock.patch(
            "vemu.service.snapshots_create"
        ) as create:
            manager.return_value.info.return_value = info
            manifest = service._ensure_seamless_rollback(
                "vm1",
                backend,
                state_dir=self.state_root,
                uri="qemu:///system",
            )
        self.assertEqual(manifest["layer"], "0002")
        create.assert_not_called()
        self.assertTrue(
            service._rollback_manifest_path("vm1").is_file()
        )



class SeamlessLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(
            "os.environ", {"XDG_STATE_HOME": self.temp.name}
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        status = SimpleNamespace(
            level="ok",
            message="FreeRDP 3.33.1",
            as_dict=lambda: {
                "binary": "/usr/bin/xfreerdp",
                "version": "3.33.1",
                "level": "ok",
                "message": "FreeRDP 3.33.1",
            },
        )
        for name in ("ensure_freerdp_compatible", "freerdp_status"):
            freerdp = mock.patch(
                f"vemu.service.{name}", return_value=status
            )
            freerdp.start()
            self.addCleanup(freerdp.stop)
        self.state_file = (
            Path(self.temp.name) / "vemu" / "seamless" / "vm1.json"
        )
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text(json.dumps(STATE))

    def test_launch_starts_supervisor(self) -> None:
        with mock.patch(
            "vemu.service.vm_seamless_refresh_host",
            return_value={"changed": False},
        ), mock.patch(
            "vemu.service.supervisor_pid", return_value=None
        ), mock.patch(
            "vemu.service._seamless_ssh", return_value=object()
        ), mock.patch(
            "vemu.service.ensure_seed",
            return_value={
                "program": "C:\\Windows\\System32\\wscript.exe",
                "arguments": "C:\\seed.vbs",
            },
        ), mock.patch(
            "vemu.service.wait_for_session", return_value=True
        ), mock.patch(
            "vemu.service.launch_in_session",
            return_value={"task": "vemu-launch-x"},
        ), mock.patch(
            "vemu.service.start_supervisor", return_value=123
        ) as start:
            result = service.vm_seamless_launch_app(
                "vm1", "Notepad", display=":0", password="secret"
            )
        start.assert_called_once()
        self.assertEqual(result["mode"], "supervisor")
        self.assertEqual(result["supervisor_pid"], 123)

    def test_launch_store_uses_direct_activator(self) -> None:
        state = json.loads(self.state_file.read_text())
        state["apps"] = [
            {
                "name": "Calculator",
                "path": "C:\\Windows\\explorer.exe",
                "arguments": (
                    "shell:AppsFolder\\"
                    "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
                ),
            }
        ]
        self.state_file.write_text(json.dumps(state))
        with mock.patch(
            "vemu.service.vm_seamless_refresh_host",
            return_value={"changed": False},
        ), mock.patch(
            "vemu.service.supervisor_pid", return_value=99
        ), mock.patch(
            "vemu.service._seamless_ssh", return_value=object()
        ), mock.patch(
            "vemu.service.ensure_activator",
            return_value=r"C:\ProgramData\vEMU\guest\vemu-activate.exe",
        ), mock.patch(
            "vemu.service.launch_in_session",
            return_value={"task": "vemu-launch-x"},
        ) as launch:
            result = service.vm_seamless_launch_app(
                "vm1", "Calculator", display=":0", password="secret"
            )
        self.assertTrue(result["direct_activation"])
        args = launch.call_args.args
        self.assertEqual(
            args[3], r"C:\ProgramData\vEMU\guest\vemu-activate.exe"
        )
        self.assertEqual(
            args[4], "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
        )

    def test_launch_uses_existing_session(self) -> None:
        with mock.patch(
            "vemu.service.vm_seamless_refresh_host",
            return_value={"changed": False},
        ), mock.patch(
            "vemu.service.supervisor_pid", return_value=99
        ), mock.patch(
            "vemu.service._seamless_ssh", return_value=object()
        ), mock.patch(
            "vemu.service.launch_in_session",
            return_value={"task": "vemu-launch-x", "output": ""},
        ) as launch:
            result = service.vm_seamless_launch_app(
                "vm1", "Notepad", display=":0", password="secret"
            )
        launch.assert_called_once()
        self.assertEqual(result["mode"], "session")
        self.assertEqual(result["supervisor_pid"], 99)

    def test_launch_requires_password(self) -> None:
        with mock.patch(
            "vemu.service.vm_seamless_refresh_host",
            return_value={"changed": False},
        ), mock.patch(
            "vemu.service.supervisor_pid", return_value=None
        ), mock.patch(
            "vemu.service.get_rdp_password", return_value=None
        ):
            with self.assertRaises(Conflict) as caught:
                service.vm_seamless_launch_app(
                    "vm1", "Notepad", display=":0"
                )
        self.assertIn("saved RDP password", str(caught.exception))

    def test_stop_supervisor(self) -> None:
        with mock.patch(
            "vemu.service.stop_supervisor", return_value=True
        ):
            result = service.vm_seamless_stop("vm1")
        self.assertTrue(result["supervisor_stopped"])

    def test_sync_shortcuts_repairs_stale_rail_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "vemu" / "seamless"
            state_dir.mkdir(parents=True)
            shortcut_dir = root / "applications"
            shortcut_dir.mkdir()
            (state_dir / "vm1.json").write_text(
                json.dumps(
                    {
                        "apps": [
                            {"name": "Excel", "path": "C:\\Excel.exe"}
                        ],
                        "desktop": "kde",
                    }
                ),
                encoding="utf-8",
            )
            stale = (
                shortcut_dir
                / "vemu-seamless-vm2-Notepad++.desktop"
            )
            stale.write_text(
                "[Desktop Entry]\n"
                "Name=Notepad++\n"
                "X-vEMU-Seamless-Domain=vm2\n"
                "StartupWMClass=RAIL\n",
                encoding="utf-8",
            )
            with mock.patch(
                "vemu.service._seamless_ssh",
                side_effect=service.VemuError("offline"),
            ):
                result = service.vm_seamless_sync_shortcuts(
                    "vm1",
                    state_dir=state_dir,
                    shortcut_dir=shortcut_dir,
                    desktop="kde",
                )
            self.assertNotIn("StartupWMClass", stale.read_text())
            self.assertIn(str(stale), result["repaired"])


class DaemonWiringTests(unittest.TestCase):
    def test_read_only_methods_are_declared(self) -> None:
        from vemu.daemon import READ_ONLY_METHODS

        self.assertIn("vm.seamless_status", READ_ONLY_METHODS)
        self.assertIn("vm.seamless_apps", READ_ONLY_METHODS)
        self.assertNotIn("vm.seamless_prepare", READ_ONLY_METHODS)


class CliParsingTests(unittest.TestCase):
    def test_app_pairs(self) -> None:
        from vemu.cli import _seamless_apps

        args = SimpleNamespace(
            app=["Notepad=C:\\Windows\\notepad.exe", "Paint=C:\\paint.exe"]
        )
        self.assertEqual(
            _seamless_apps(args),
            {
                "Notepad": "C:\\Windows\\notepad.exe",
                "Paint": "C:\\paint.exe",
            },
        )

    def test_app_pair_requires_equals(self) -> None:
        from vemu.cli import _seamless_apps

        with self.assertRaises(service.ValidationError):
            _seamless_apps(SimpleNamespace(app=["Notepad"]))
