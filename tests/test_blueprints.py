from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "templates"


def load(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def files(data: dict) -> list[dict]:
    return list(data.get("customizations", {}).get("files", []))


class BlueprintTemplateTests(unittest.TestCase):
    def test_all_blueprints_parse(self) -> None:
        paths = sorted(ROOT.glob("*/blueprint*.toml"))
        self.assertGreaterEqual(len(paths), 10)
        for path in paths:
            with self.subTest(path=path):
                data = load(path)
                self.assertTrue(data.get("name"))
                self.assertTrue(data.get("distro"))
                self.assertTrue(data.get("version"))

    def test_qemu_variants_have_debloat_unit(self) -> None:
        for path in sorted(ROOT.glob("*/blueprint-qemu.toml")):
            with self.subTest(path=path):
                data = load(path)
                self.assertTrue(data["name"].endswith("-qemu"))
                services = data["customizations"]["services"]["enabled"]
                self.assertIn("vemu-debloat.service", services)
                paths = {item["path"]: item for item in files(data)}
                script = paths["/usr/local/sbin/vemu-debloat"]["data"]
                self.assertIn("-firmware$", script)
                self.assertIn("kdump-utils", script)
                self.assertIn("mcelog", script)
                self.assertIn(
                    "/etc/systemd/system/vemu-debloat.service", paths
                )

    def test_rhel_templates_have_dhcp_profile(self) -> None:
        for path in sorted((ROOT / "rhel-10.3-gnome").glob("blueprint*.toml")):
            with self.subTest(path=path):
                paths = {item["path"] for item in files(load(path))}
                self.assertIn(
                    "/etc/NetworkManager/system-connections/"
                    "vemu-ethernet.nmconnection",
                    paths,
                )

    def test_rhel_kde_uses_epel_and_plasmalogin(self) -> None:
        data = load(ROOT / "rhel-10.3-kde" / "blueprint.toml")
        repos = {
            repo["id"]
            for repo in data.get("customizations", {}).get("repositories", [])
        }
        self.assertIn("epel", repos)
        services = data["customizations"]["services"]["enabled"]
        self.assertIn("plasmalogin", services)
        self.assertNotIn("sddm", services)


if __name__ == "__main__":
    unittest.main()
