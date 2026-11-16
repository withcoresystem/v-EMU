"""ISO post-processing for vEMU.

The image-builder blueprint stays the source of truth for the OS payload.  This
module only adds the optional vEMU layers on top of the generated installer
ISO:

* branded Anaconda (updates.img, no install.img repack)
* branded GRUB menu
* an interactive kickstart with cleanup
* optional first-boot TUI payload embedded through kickstart %post

The module is also a root entry point used by the Image Builder job:

    python3 vemu/iso_builder.py --job-dir ... --blueprint ... --distro ...
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
PLYMOUTH_CREDIT = "Powered by v-EMU"
FIRSTBOOT_SOURCES = ("usb", "url", "bundled")

FIRSTBOOT_RUN = r"""#!/bin/bash
set -e
mkdir -p /run/vemu-firstboot
export TERM=linux
/usr/bin/chvt 2 >/dev/null 2>&1 || true
cp -f /usr/libexec/vemu-firstboot/loader.py /run/vemu-firstboot/loader.py
exec /usr/bin/python3 /run/vemu-firstboot/loader.py
"""

FIRSTBOOT_CLEANUP = r"""#!/bin/bash
set -u
rm -f /etc/systemd/system/vemu-firstboot.service
rm -f /etc/systemd/system/multi-user.target.wants/vemu-firstboot.service
rm -rf /usr/libexec/vemu-firstboot /usr/libexec/vemu-firstboot.d /etc/vemu
rm -rf /run/vemu-firstboot/payload /run/vemu-firstboot/mnt
/usr/bin/chvt 1 >/dev/null 2>&1 || true
exit 0
"""

FIRSTBOOT_UNIT = """[Unit]
Description=vEMU First Boot
ConditionPathExists=!/var/lib/vemu/firstboot.done
ConditionPathExists=!/var/lib/vemu/firstboot.skipped
After=systemd-user-sessions.service network.target
Before=display-manager.service
Conflicts=getty@tty2.service

[Service]
Type=oneshot
Environment=TERM=linux
ExecStart=/usr/libexec/vemu-firstboot/run
ExecStopPost=-/bin/bash /usr/libexec/vemu-firstboot/cleanup
StandardInput=tty
StandardOutput=tty
StandardError=tty
TTYPath=/dev/tty2
TTYReset=yes
TTYVHangup=yes
TTYVTDisallocate=yes
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
"""


@dataclass(frozen=True)
class IsoBranding:
    product_name: str
    product_version: str
    sidebar_bg: str = "#111827"
    accent: str = "#0f766e"
    logo_svg: str = ""
    grub_title: str = ""
    grub_timeout: int = 15

    @property
    def title(self) -> str:
        return self.grub_title.strip() or f"Install {self.product_name}"


@dataclass(frozen=True)
class IsoFirstboot:
    company_name: str = "CORESYSTEM"
    sources: tuple[str, ...] = ("usb", "url")
    retries: int = 3
    allow_skip: bool = True
    reboot_prompt: bool = True
    script_name: str = ""
    script_url: str = ""


@dataclass(frozen=True)
class IsoOptions:
    branding: IsoBranding | None = None
    firstboot: IsoFirstboot | None = None

    @property
    def active(self) -> bool:
        return self.branding is not None or self.firstboot is not None


def _hex_luminance(color: str) -> float:
    color = color.lstrip("#")
    channels = [int(color[i : i + 2], 16) / 255.0 for i in (0, 2, 4)]
    linear = [
        value / 12.92
        if value <= 0.04045
        else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_text(background: str) -> str:
    return "#0b1220" if _hex_luminance(background) > 0.45 else "#ffffff"


def validate_options(options: IsoOptions) -> None:
    """Validate user-provided code before a build job is started."""

    branding = options.branding
    if branding is not None:
        if not branding.product_name.strip():
            raise ValueError("Branding: product name is required.")
        if not branding.product_version.strip():
            raise ValueError("Branding: product version is required.")
        for label, value in (
            ("sidebar background", branding.sidebar_bg),
            ("accent", branding.accent),
        ):
            if not HEX_RE.match(value):
                raise ValueError(f"Branding: {label} must be #RRGGBB.")
        if not branding.logo_svg.strip():
            raise ValueError("Branding: SVG logo code is required.")
        if len(branding.logo_svg.encode("utf-8")) > 512 * 1024:
            raise ValueError("Branding: SVG is larger than 512 KiB.")
        try:
            root = ET.fromstring(branding.logo_svg)
        except ET.ParseError as exc:
            raise ValueError(f"Branding: SVG is not valid XML: {exc}") from exc
        if str(root.tag).split("}")[-1].lower() != "svg":
            raise ValueError("Branding: SVG root element must be <svg>.")
        if branding.grub_timeout < 0 or branding.grub_timeout > 600:
            raise ValueError("Branding: GRUB timeout must be 0..600 seconds.")

    firstboot = options.firstboot
    if firstboot is not None:
        if not firstboot.company_name.strip():
            raise ValueError("Firstboot: company name is required.")
        if not firstboot.sources:
            raise ValueError("Firstboot: choose at least one source.")
        unknown = set(firstboot.sources) - set(FIRSTBOOT_SOURCES)
        if unknown:
            raise ValueError(
                f"Firstboot: unsupported source(s): {', '.join(sorted(unknown))}."
            )
        if firstboot.retries < 0 or firstboot.retries > 20:
            raise ValueError("Firstboot: retries must be 0..20.")
        script_name = firstboot.script_name.strip()
        script_url = firstboot.script_url.strip()
        if script_name:
            if len(script_name) > 128 or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]*", script_name
            ):
                raise ValueError(
                    "Firstboot: script name must be a simple file name "
                    "(letters, digits, dot, dash, underscore)."
                )
        if script_url:
            parsed = urlsplit(script_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError(
                    "Firstboot: script URL must be an HTTP(S) URL."
                )
            if script_name and not parsed.path.endswith("/"):
                raise ValueError(
                    "Firstboot: script URL must end with '/' when a script "
                    "name is set."
                )
            if not script_name and parsed.path.endswith("/"):
                raise ValueError(
                    "Firstboot: a directory URL needs a script name."
                )


def _run(
    command: list[str], *, cwd: Path | None = None, quiet: bool = False
) -> None:
    if not quiet:
        print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def _logo_png(svg: str, output: Path, workdir: Path) -> None:
    svg_path = workdir / "logo.svg"
    svg_path.write_text(svg, encoding="utf-8")
    magick = shutil.which("magick") or shutil.which("convert")
    if not magick:
        raise RuntimeError("ImageMagick (magick/convert) is required for SVG rendering.")
    temporary = workdir / "logo-69.png"
    _run([magick, "-background", "none", str(svg_path), "-resize", "150x69", str(temporary)])
    _run(
        [
            magick,
            str(temporary),
            "-background",
            "none",
            "-gravity",
            "center",
            "-extent",
            "150x69",
            str(output),
        ]
    )


def _mix_color(fg: str, bg: str, alpha: float) -> str:
    f = [int(fg.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    b = [int(bg.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4)]
    values = [round(fc * alpha + bc * (1.0 - alpha)) for fc, bc in zip(f, b)]
    return "#" + "".join(f"{value:02x}" for value in values)


def _font_path() -> str:
    fc_match = shutil.which("fc-match")
    if fc_match:
        try:
            result = subprocess.run(
                [fc_match, "-f", "%{file}", "DejaVu Sans"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            candidate = result.stdout.strip()
            if candidate and Path(candidate).is_file():
                return candidate
        except (OSError, subprocess.SubprocessError):
            pass
    for candidate in (
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(candidate).is_file():
            return candidate
    return ""


def _plymouth_spinner(magick: str, theme_dir: Path, accent: str) -> None:
    """Create rotating progress-####.png frames for the boot animation."""
    frames = 30
    dots = 14
    size = 72
    radius = 22.0
    center = size / 2.0
    red, green, blue = (int(accent.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    for frame in range(frames):
        head = frame * (360.0 / frames)
        commands = []
        for dot in range(dots):
            angle = math.radians(head - dot * 18.0)
            alpha = 0.95 * (1.0 - dot / (dots + 1.0)) + 0.04
            x = center + radius * math.cos(angle)
            y = center + radius * math.sin(angle)
            dot_radius = 3.4 - dot * 0.08
            commands.append(
                f"fill rgba({red},{green},{blue},{alpha:.3f}) "
                f"circle {x:.2f},{y:.2f} {x + dot_radius:.2f},{y:.2f}"
            )
        _run(
            [
                magick,
                "-size",
                f"{size}x{size}",
                "xc:none",
                "-draw",
                " ".join(commands),
                f"PNG32:{theme_dir / f'progress-{frame + 1:04d}.png'}",
            ],
            quiet=True,
        )


def _plymouth_widgets(magick: str, theme_dir: Path) -> None:
    """Create the images two-step requires before it will load a theme.

    The plugin always loads lock.png, entry.png and bullet.png; a missing
    image makes it fall back to the text splash. capslock/keyboard are
    optional but keep the password prompt from logging load errors.
    """
    _run(
        [
            magick,
            "-size",
            "35x34",
            "xc:none",
            "-fill",
            "#9ca3af",
            "-draw",
            "roundrectangle 6,15 29,32 4,4",
            "-stroke",
            "#9ca3af",
            "-strokewidth",
            "3",
            "-fill",
            "none",
            "-draw",
            "path 'M 10,15 L 10,9 A 7.5,7.5 0 0 1 25,9 L 25,15'",
            f"PNG32:{theme_dir / 'lock.png'}",
        ]
    )
    _run(
        [
            magick,
            "-size",
            "305x34",
            "xc:none",
            "-fill",
            "rgba(17,24,39,0.85)",
            "-draw",
            "roundrectangle 0,0 304,33 8,8",
            "-stroke",
            "rgba(148,163,184,0.7)",
            "-strokewidth",
            "1",
            "-fill",
            "none",
            "-draw",
            "roundrectangle 0.5,0.5 303.5,32.5 8,8",
            f"PNG32:{theme_dir / 'entry.png'}",
        ]
    )
    _run(
        [
            magick,
            "-size",
            "8x8",
            "xc:none",
            "-fill",
            "white",
            "-draw",
            "circle 4,4 4,1",
            f"PNG32:{theme_dir / 'bullet.png'}",
        ]
    )
    for name in ("capslock.png", "keyboard.png"):
        _run([magick, "-size", "1x1", "xc:none", f"PNG32:{theme_dir / name}"])


def make_plymouth_theme(branding: IsoBranding, workdir: Path) -> list[tuple[str, bytes, int]]:
    """Generate a two-step Plymouth theme with logo, credit and spinner.

    The credit is part of watermark.png and the spinner is a progress-*
    animation; no Plymouth script plugin is used.
    """
    theme_dir = workdir / "plymouth" / "vemu"
    theme_dir.mkdir(parents=True, exist_ok=True)
    magick = shutil.which("magick") or shutil.which("convert")
    if not magick:
        raise RuntimeError("ImageMagick (magick/convert) is required for Plymouth.")

    svg_path = workdir / "plymouth-logo.svg"
    svg_path.write_text(branding.logo_svg, encoding="utf-8")
    logo_path = workdir / "plymouth-logo.png"
    credit_path = workdir / "plymouth-credit.png"
    watermark_path = theme_dir / "watermark.png"

    _run(
        [
            magick,
            "-background",
            "none",
            str(svg_path),
            "-resize",
            "280x240",
            str(logo_path),
        ]
    )
    font = _font_path()
    text_command = [
        magick,
        "-background",
        "none",
        "-fill",
        "#e5e7eb",
    ]
    if font:
        text_command += ["-font", font]
    text_command += ["-pointsize", "24", "label:" + PLYMOUTH_CREDIT, str(credit_path)]
    _run(text_command)

    _run(
        [
            magick,
            "-size",
            "780x400",
            "xc:none",
            str(logo_path),
            "-gravity",
            "north",
            "-geometry",
            "+0+40",
            "-composite",
            str(credit_path),
            "-gravity",
            "north",
            "-geometry",
            "+0+296",
            "-composite",
            f"PNG32:{watermark_path}",
        ]
    )
    _plymouth_widgets(magick, theme_dir)
    spinner_color = _mix_color("#ffffff", branding.accent, 0.65)
    _plymouth_spinner(magick, theme_dir, spinner_color)

    start = branding.sidebar_bg
    end = _mix_color(branding.accent, branding.sidebar_bg, 0.22)
    theme = f"""[Plymouth Theme]
Name=vEMU
Description={PLYMOUTH_CREDIT}
ModuleName=two-step

[two-step]
ImageDir=/usr/share/plymouth/themes/vemu
HorizontalAlignment=.5
VerticalAlignment=.66
WatermarkHorizontalAlignment=.5
WatermarkVerticalAlignment=.38
DialogHorizontalAlignment=.5
DialogVerticalAlignment=.5
Transition=none
TransitionDuration=0.0
BackgroundStartColor=0x{start.lstrip('#')}
BackgroundEndColor=0x{end.lstrip('#')}
UseFirmwareBackground=false
MessageBelowAnimation=true
UseAnimation=true
UseProgressBar=false

[boot-up]
UseEndAnimation=false
UseAnimation=true

[shutdown]
UseEndAnimation=false
UseAnimation=false

[reboot]
UseEndAnimation=false
UseAnimation=false
"""
    files: list[tuple[str, bytes, int]] = [
        ("/usr/share/plymouth/themes/vemu/vemu.plymouth", theme.encode(), 0o644),
        ("/etc/plymouth/plymouthd.conf", b"[Daemon]\nTheme=vemu\n", 0o644),
    ]
    for asset in sorted(theme_dir.glob("*.png")):
        files.append(
            (f"/usr/share/plymouth/themes/vemu/{asset.name}", asset.read_bytes(), 0o644)
        )
    return files


def _cpio_gz(source: Path, output: Path) -> None:
    find = subprocess.Popen(
        ["find", ".", "-print0"], cwd=source, stdout=subprocess.PIPE
    )
    cpio = subprocess.Popen(
        ["cpio", "--null", "-o", "-H", "newc"],
        cwd=source,
        stdin=find.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert find.stdout is not None
    find.stdout.close()
    with output.open("wb") as handle:
        gz = subprocess.Popen(["gzip", "-9"], stdin=cpio.stdout, stdout=handle)
        assert cpio.stdout is not None
        cpio.stdout.close()
        if find.wait() != 0 or cpio.wait() != 0 or gz.wait() != 0:
            raise RuntimeError("cpio/gzip failed while building updates.img")


def make_updates_img(branding: IsoBranding, workdir: Path) -> Path:
    """Build the tiny Anaconda updates image; install.img is never touched."""

    root = workdir / "updates-root"
    shutil.rmtree(root, ignore_errors=True)
    (root / "run/install/updates").mkdir(parents=True)

    stamp = f"""[Main]
product = {branding.product_name}
version = {branding.product_version}
isfinal = True
uuid = {datetime.now(timezone.utc).strftime("%Y%m%d%H%M")}.x86_64
variant = KDE

[Compose]
osbuild = devel
"""
    (root / ".buildstamp").write_text(stamp, encoding="utf-8")

    text_color = contrast_text(branding.sidebar_bg)
    topbar_text = contrast_text(branding.accent)
    css = f"""/* Generated by vEMU ISO branding */
@define-color anaconda_bg_color {branding.sidebar_bg};

.logo-sidebar {{
    background-color: @anaconda_bg_color;
    background-image: none;
    color: {text_color};
}}

.logo {{
    background-image: url('/run/install/updates/sidebar-logo.png');
    background-position: 50% 24px;
    background-repeat: no-repeat;
    background-color: transparent;
}}

.product-logo {{
    background-image: none;
    background-color: transparent;
}}

AnacondaSpokeWindow #nav-box {{
    background-color: {branding.accent};
    background-image: none;
    color: {topbar_text};
}}

"""
    (root / "run/install/updates/anaconda-gtk.css").write_text(css, encoding="utf-8")
    _logo_png(branding.logo_svg, root / "run/install/updates/sidebar-logo.png", workdir)

    image = workdir / "updates.img"
    _cpio_gz(root, image)
    return image


def firstboot_files(firstboot: IsoFirstboot, loader: Path) -> list[tuple[str, bytes, int]]:
    config = json.dumps(
        {
            "company_name": firstboot.company_name,
            "sources": list(firstboot.sources),
            "url": "",
            "retries": firstboot.retries,
            "allow_skip": firstboot.allow_skip,
            "reboot_prompt": firstboot.reboot_prompt,
            "script_name": firstboot.script_name,
            "script_url": firstboot.script_url,
        },
        indent=2,
    ).encode() + b"\n"
    return [
        ("/usr/libexec/vemu-firstboot/loader.py", loader.read_bytes(), 0o644),
        ("/usr/libexec/vemu-firstboot/run", FIRSTBOOT_RUN.encode(), 0o755),
        ("/usr/libexec/vemu-firstboot/cleanup", FIRSTBOOT_CLEANUP.encode(), 0o755),
        ("/etc/systemd/system/vemu-firstboot.service", FIRSTBOOT_UNIT.encode(), 0o644),
        ("/etc/vemu/firstboot.json", config, 0o644),
    ]


def _loader_path() -> Path:
    return Path(__file__).resolve().with_name("iso_assets") / "loader.py"


def make_kickstart(
    options: IsoOptions,
    plymouth_files: list[tuple[str, bytes, int]] | None = None,
) -> str:
    lines = [
        "graphical",
        "lang en_US.UTF-8",
        "keyboard us",
        "timezone UTC",
        "firstboot --disable",
        "network --device=link --bootproto=dhcp --activate",
        'liveimg --url file:///run/install/repo/liveimg.tar.gz',
        "",
        "%post --log=/var/log/vemu-post.log",
        "set -e",
        "mkdir -p /usr/libexec/vemu-firstboot /usr/libexec/vemu-firstboot.d "
        "/var/lib/vemu /etc/vemu /etc/systemd/system/multi-user.target.wants "
        "/etc/skel/.config /usr/share/plymouth/themes/vemu /etc/plymouth",
        'printf "done\\n" > /etc/skel/.config/gnome-initial-setup-done',
    ]

    if plymouth_files:
        for index, (path, data, _mode) in enumerate(plymouth_files):
            marker = f"VEMU_PLYMOUTH_{index}"
            encoded = base64.encodebytes(data).decode("ascii").rstrip()
            lines += [f"base64 -d > {path} <<'{marker}'", encoded, marker]
        lines += [
            "if command -v plymouth-set-default-theme >/dev/null 2>&1; then "
            "plymouth-set-default-theme -R vemu || true; "
            "plymouth-set-default-theme vemu || true; fi",
            "if command -v dracut >/dev/null 2>&1; then dracut -f || true; fi",
            "if command -v grubby >/dev/null 2>&1; then "
            "grubby --update-kernel=ALL "
            "--args='rhgb quiet systemd.show_status=no "
            "vt.global_cursor_default=0 plymouth.graphical bgrt_disable' "
            "|| true; fi",
        ]

    if options.firstboot is not None:
        files = firstboot_files(options.firstboot, _loader_path())
        for index, (path, data, _mode) in enumerate(files):
            marker = f"VEMU_FILE_{index}"
            encoded = base64.encodebytes(data).decode("ascii").rstrip()
            lines += [f"base64 -d > {path} <<'{marker}'", encoded, marker]
        lines += [
            "chmod 755 /usr/libexec/vemu-firstboot/run "
            "/usr/libexec/vemu-firstboot/cleanup",
            "ln -sf /etc/systemd/system/vemu-firstboot.service "
            "/etc/systemd/system/multi-user.target.wants/vemu-firstboot.service",
        ]

    lines += [
        "ln -sf /usr/lib/systemd/system/graphical.target /etc/systemd/system/default.target",
        "touch /etc/plasma-setup-done",
        "rm -f /etc/systemd/system/*.wants/plasma-setup.service",
        "for home in /home/*; do "
        '[ -d "$home" ] && mkdir -p "$home/.config" && '
        'printf "done\\n" > "$home/.config/gnome-initial-setup-done"; '
        "done",
        "dnf -y --disablerepo='*' remove anaconda-core anaconda-tui || true",
        "%end",
        "",
    ]
    return "\n".join(lines)


def prepare_job(job_dir: Path, options: IsoOptions, *, output: Path | None = None) -> dict:
    """Create user-owned inputs for the privileged ISO job."""

    validate_options(options)
    job_dir.mkdir(parents=True, exist_ok=True)
    workdir = job_dir / "customizer"
    shutil.rmtree(workdir, ignore_errors=True)
    workdir.mkdir()

    updates = make_updates_img(options.branding, workdir) if options.branding else None
    if updates is not None:
        shutil.copy2(updates, job_dir / "updates.img")
        updates = job_dir / "updates.img"
    plymouth_files = (
        make_plymouth_theme(options.branding, workdir) if options.branding else None
    )
    ks = job_dir / "vemu.ks"
    ks.write_text(make_kickstart(options, plymouth_files), encoding="utf-8")
    options_payload = {
        "branding": asdict(options.branding) if options.branding else None,
        "firstboot": asdict(options.firstboot) if options.firstboot else None,
        "output": str(output) if output else "",
    }
    (job_dir / "iso-options.json").write_text(
        json.dumps(options_payload, indent=2), encoding="utf-8"
    )
    return {"kickstart": ks, "updates": updates, "options": job_dir / "iso-options.json"}


def load_options(path: Path) -> IsoOptions:
    data = json.loads(path.read_text(encoding="utf-8"))
    branding = IsoBranding(**data["branding"]) if data.get("branding") else None
    firstboot_data = data.get("firstboot")
    if firstboot_data:
        firstboot_data["sources"] = tuple(firstboot_data.get("sources") or ())
        firstboot = IsoFirstboot(**firstboot_data)
    else:
        firstboot = None
    return IsoOptions(branding=branding, firstboot=firstboot)


def _read_iso_file(iso: Path, member: str, output: Path) -> bool:
    return subprocess.run(
        ["xorriso", "-osirrox", "on", "-indev", str(iso), "-extract", member, str(output)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def grub_replacements(base: Path, workdir: Path, branding: IsoBranding) -> list[tuple[str, str]]:
    grub = workdir / "base-grub.cfg"
    if not _read_iso_file(base, "/EFI/BOOT/grub.cfg", grub):
        raise RuntimeError("Cannot read the base ISO EFI grub.cfg")
    text = grub.read_text(errors="replace")
    entries = re.findall(r"menuentry\s+'([^']+)'", text)
    title = branding.title
    product = branding.product_name
    replacements: list[tuple[str, str]] = []
    for entry in entries:
        lower = entry.lower()
        if "test this media" in lower or "test media" in lower:
            new = f"Test media & install {product}"
        elif "basic graphics" in lower:
            new = f"{title} (basic graphics)"
        elif "fips" in lower:
            new = f"{title} (FIPS)"
        elif "rescue" in lower:
            new = f"Rescue a {product} system"
        elif "install" in lower:
            new = title
        else:
            continue
        if entry != new:
            replacements.append((entry, new))

    default = re.search(r'set default="[^"]*"', text)
    if default:
        replacements.append((default.group(0), 'set default="0"'))
    timeout = re.search(r"set timeout=[0-9]+", text)
    if timeout:
        replacements.append((timeout.group(0), f"set timeout={branding.grub_timeout}"))
    return replacements


def postprocess(
    base_iso: Path,
    output: Path,
    kickstart: Path,
    updates: Path | None,
    options: IsoOptions,
) -> None:
    workdir = output.parent / "postprocess"
    workdir.mkdir(parents=True, exist_ok=True)
    command = ["mkksiso", "--ks", str(kickstart)]
    if updates is not None:
        command += ["--updates", str(updates)]
    if options.branding is not None:
        for old, new in grub_replacements(base_iso, workdir, options.branding):
            command += ["-R", old, new]
    command += [str(base_iso), str(output)]
    _run(command)


def image_type(distro: str) -> str:
    name = distro.lower()
    if name.startswith("fedora"):
        return "minimal-installer"
    if any(
        item in name for item in ("rhel", "almalinux", "rocky", "centos", "oracle")
    ):
        return "image-installer"
    raise ValueError(f"Cannot choose an ISO image type for distro {distro!r}.")


def _find_iso(directory: Path) -> Path:
    candidates = sorted(directory.rglob("*.iso"))
    if not candidates:
        raise RuntimeError(f"image-builder produced no ISO in {directory}")
    return max(candidates, key=lambda item: item.stat().st_size)


def cleanup_job_dir(job_dir: Path, *, keep: list[Path]) -> None:
    """Remove build intermediates, keeping the artifact and build log.

    Runs as root in the ISO helper, so it can remove the base ISO copy and the
    Anaconda/Plymouth work directories. Failures are warnings: a successful
    build must not fail because a temporary file could not be removed.
    """
    keep_paths = {path.resolve() for path in keep}
    for entry in list(job_dir.iterdir()):
        try:
            if entry.resolve() in keep_paths:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink(missing_ok=True)
        except OSError as exc:
            print(
                f"warning: cannot remove build intermediate {entry}: {exc}",
                flush=True,
            )


def run_job(
    job_dir: Path,
    blueprint: Path,
    distro: str,
    output: Path,
    options: IsoOptions,
    *,
    extra_repos: list[str] | None = None,
    repo_dir: str | None = None,
) -> None:
    base_dir = job_dir / "base"
    shutil.rmtree(base_dir, ignore_errors=True)
    base_dir.mkdir(parents=True)
    command = [
        "image-builder",
        "build",
        image_type(distro),
        "--distro",
        distro,
        "--blueprint",
        str(blueprint),
        "--output-dir",
        str(base_dir),
    ]
    if repo_dir:
        command += ["--force-repo-dir", repo_dir]
    for url in extra_repos or []:
        command += ["--extra-repo", url]
    _run(command)

    base_iso = _find_iso(base_dir)
    ks = job_dir / "vemu.ks"
    updates = job_dir / "updates.img" if options.branding else None
    if not ks.is_file():
        raise RuntimeError(f"Missing prepared kickstart: {ks}")
    if updates is not None and not updates.is_file():
        raise RuntimeError(f"Missing prepared updates image: {updates}")
    postprocess(base_iso, output, ks, updates, options)
    cleanup_job_dir(job_dir, keep=[output, job_dir / "build.log"])
    print(f"ISO: {output}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a vEMU-customized ISO")
    parser.add_argument("--job-dir", type=Path, required=True)
    parser.add_argument("--blueprint", type=Path, required=True)
    parser.add_argument("--distro", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--options", type=Path, required=True)
    parser.add_argument("--extra-repo", action="append", default=[])
    parser.add_argument("--force-repo-dir")
    args = parser.parse_args(argv)
    options = load_options(args.options)
    try:
        run_job(
            args.job_dir,
            args.blueprint,
            args.distro,
            args.output,
            options,
            extra_repos=args.extra_repo,
            repo_dir=args.force_repo_dir,
        )
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
