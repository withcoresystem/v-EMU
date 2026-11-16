from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from vemu.iso_builder import (
    IsoBranding,
    IsoFirstboot,
    IsoOptions,
    cleanup_job_dir,
    image_type,
    load_options,
    make_kickstart,
    make_plymouth_theme,
    validate_options,
)

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
    '<rect width="16" height="16" fill="#0f766e"/></svg>'
)


def options(*, branding: bool = True, firstboot: bool = True) -> IsoOptions:
    return IsoOptions(
        branding=(
            IsoBranding(
                product_name="vEMU Lab",
                product_version="0.2",
                sidebar_bg="#111827",
                accent="#0f766e",
                logo_svg=SVG,
                grub_title="Install vEMU Lab",
            )
            if branding
            else None
        ),
        firstboot=(
            IsoFirstboot(
                company_name="CORESYSTEM",
                sources=("usb", "url"),
                retries=3,
            )
            if firstboot
            else None
        ),
    )


class IsoBuilderTests(unittest.TestCase):
    def test_image_type(self) -> None:
        self.assertEqual(image_type("fedora-45"), "minimal-installer")
        self.assertEqual(image_type("almalinux-10.2"), "image-installer")
        self.assertEqual(image_type("rhel-10.3"), "image-installer")
        with self.assertRaises(ValueError):
            image_type("debian-13")

    def test_validate_branding(self) -> None:
        validate_options(options())
        with self.assertRaises(ValueError):
            validate_options(
                IsoOptions(
                    branding=IsoBranding(
                        product_name="vEMU",
                        product_version="0.2",
                        sidebar_bg="111827",
                        accent="#0f766e",
                        logo_svg=SVG,
                    )
                )
            )
        with self.assertRaises(ValueError):
            validate_options(
                IsoOptions(
                    branding=IsoBranding(
                        product_name="vEMU",
                        product_version="0.2",
                        sidebar_bg="#111827",
                        accent="#0f766e",
                        logo_svg="<not-svg/>",
                    )
                )
            )

    def test_validate_firstboot(self) -> None:
        with self.assertRaises(ValueError):
            validate_options(
                IsoOptions(
                    firstboot=IsoFirstboot(
                        company_name="",
                        sources=("usb",),
                    )
                )
            )
        with self.assertRaises(ValueError):
            validate_options(
                IsoOptions(
                    firstboot=IsoFirstboot(
                        company_name="CORESYSTEM",
                        sources=("nfs",),
                    )
                )
            )

    def test_kickstart_with_firstboot(self) -> None:
        ks = make_kickstart(options())
        self.assertIn("liveimg --url file:///run/install/repo/liveimg.tar.gz", ks)
        self.assertIn(
            "network --device=link --bootproto=dhcp --activate", ks
        )
        self.assertIn("base64 -d > /usr/libexec/vemu-firstboot/loader.py", ks)
        self.assertIn("vemu-firstboot.service", ks)
        self.assertIn("dnf -y --disablerepo='*' remove anaconda-core anaconda-tui", ks)
        self.assertNotIn("tar -xzf", ks)

    def test_automatic_firstboot_payload_validation(self) -> None:
        base = IsoFirstboot(company_name="CORESYSTEM", sources=("usb", "url"))
        validate_options(
            IsoOptions(
                firstboot=replace(
                    base,
                    script_name="site-setup.sh",
                    script_url="https://deploy.example/firstboot/",
                )
            )
        )
        validate_options(
            IsoOptions(
                firstboot=replace(
                    base, script_url="https://deploy.example/site-setup.sh"
                )
            )
        )
        with self.assertRaises(ValueError):
            validate_options(
                IsoOptions(
                    firstboot=replace(
                        base,
                        script_name="site-setup.sh",
                        script_url="https://deploy.example/site-setup.sh",
                    )
                )
            )
        with self.assertRaises(ValueError):
            validate_options(
                IsoOptions(
                    firstboot=replace(
                        base, script_url="https://deploy.example/firstboot/"
                    )
                )
            )
        with self.assertRaises(ValueError):
            validate_options(
                IsoOptions(
                    firstboot=replace(base, script_name="../site-setup.sh")
                )
            )

    def test_kickstart_cleanup_only(self) -> None:
        ks = make_kickstart(IsoOptions())
        self.assertIn("anaconda-core", ks)
        self.assertNotIn("vemu-firstboot.service", ks)

    def test_kickstart_with_plymouth(self) -> None:
        ks = make_kickstart(
            options(firstboot=False),
            [("/usr/share/plymouth/themes/vemu/lock.png", b"png", 0o644)],
        )
        self.assertIn(
            "base64 -d > /usr/share/plymouth/themes/vemu/lock.png", ks
        )
        self.assertIn("plymouth-set-default-theme vemu", ks)
        self.assertIn("dracut -f", ks)
        self.assertIn("plymouth.graphical", ks)
        self.assertIn("bgrt_disable", ks)
        self.assertIn("rhgb quiet systemd.show_status=no", ks)
        self.assertNotIn("vemu-firstboot.service", ks)

    @unittest.skipUnless(
        shutil.which("magick") or shutil.which("convert"),
        "ImageMagick is required",
    )
    def test_plymouth_theme_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            files = make_plymouth_theme(options().branding, Path(tmp))
        paths = {path for path, _data, _mode in files}
        for name in (
            "vemu.plymouth",
            "watermark.png",
            "lock.png",
            "entry.png",
            "bullet.png",
            "capslock.png",
            "keyboard.png",
        ):
            self.assertIn(f"/usr/share/plymouth/themes/vemu/{name}", paths)
        theme = next(data for path, data, _ in files if path.endswith(".plymouth"))
        self.assertIn(b"ModuleName=two-step", theme)
        self.assertIn(b"Description=Powered by v-EMU", theme)
        self.assertIn(b"VerticalAlignment=.66", theme)
        self.assertTrue(
            any(path.endswith("progress-0001.png") for path in paths)
        )

    def test_cleanup_job_dir_keeps_artifact_and_log(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            job = Path(tmp)
            artifact = job / "final.iso"
            artifact.write_bytes(b"iso")
            log = job / "build.log"
            log.write_text("log", encoding="utf-8")
            (job / "base").mkdir()
            (job / "base" / "base.iso").write_bytes(b"base")
            (job / "customizer").mkdir()
            (job / "updates.img").write_bytes(b"updates")
            (job / "vemu.ks").write_text("ks", encoding="utf-8")
            cleanup_job_dir(job, keep=[artifact, log])
            self.assertTrue(artifact.is_file())
            self.assertTrue(log.is_file())
            self.assertEqual(
                sorted(item.name for item in job.iterdir()),
                ["build.log", "final.iso"],
            )

    def test_load_options_roundtrip(self) -> None:
        original = options()
        payload = {
            "branding": asdict(original.branding) if original.branding else None,
            "firstboot": asdict(original.firstboot) if original.firstboot else None,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "options.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = load_options(path)
        self.assertEqual(loaded.branding, original.branding)
        self.assertEqual(loaded.firstboot, original.firstboot)


if __name__ == "__main__":
    unittest.main()
