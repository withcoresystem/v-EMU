"""Experimental Windows 3D conversion using the pinned vgpu3d driver.

This module is intentionally CLI-only and unsupported.  It converts one
Windows 11 VM from the normal VNC/QXL console to the validated
``virtio-vga-gl`` + SPICE/EGL-headless profile, installs the pinned
``vgpu3d`` test-signed driver in the guest, and records everything needed for
``vemu experimental 3d revert``.

The seamless helper and this module share low-level primitives (QGA, SSH,
snapshots, XML backups) but not the RDP/RemoteApp pipeline: vgpu3d is a
console/SPICE feature and is not compatible with seamless publishing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .backend import LibvirtBackend
from .chain import ChainManager
from .errors import Conflict, NotFound, ValidationError, VemuError
from .external_spice import find_spiceviewer, viewer_install_hint
from .guest.qga import QGATransport
from .guest.ssh import SSHTransport, ensure_keypair, scan_host_key
from .seamless.rdp import install_admin_key, install_user_key

RELEASE = "v1.0.26313.18562"
DRIVER_VERSION = "1.0.26313.18562"
SETUP_NAME = "vgpu3d-setup-1.0.26313.18562.exe"
SETUP_SHA256 = (
    "23f9b53d9b2c3bc4b5c6862b5b790a2c09f9e7d0f2063cddc71032476532ef94"
)
DOWNLOAD_BASE = (
    "https://github.com/fastchain/vgpu3d/releases/download/" + RELEASE + "/"
)
SNAPSHOT_LABEL = "experimental-3d"
QEMU_CONF = Path("/etc/libvirt/qemu.conf")
QEMU_CONF_BACKUP = Path("/etc/libvirt/qemu.conf.vemu-3d-backup")
QEMU_NS = "http://libvirt.org/schemas/domain/qemu/1.0"
ET.register_namespace("qemu", QEMU_NS)
STATE_VERSION = 1
OFF_VALUES = {"0", "false", "off", "no"}
ON_VALUES = {"1", "true", "on", "yes"}
_UNSET = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_shared_dir(path: Path) -> None:
    if os.geteuid() != 0:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return
    try:
        path.mkdir(mode=0o770, parents=True, exist_ok=True)
        import grp

        try:
            group = grp.getgrnam("vemu").gr_gid
        except KeyError:
            group = None
        if group is not None:
            os.chown(path, -1, group)
        os.chmod(path, 0o770)
    except OSError:
        pass


def experimental_state_dir() -> Path:
    """Return the state directory used by the daemon (or by --direct runs)."""
    override = os.environ.get("VEMU_EXPERIMENTAL_STATE_DIR")
    if override:
        return Path(override).expanduser()
    if os.geteuid() == 0:
        path = Path("/var/lib/vemu/experimental3d")
        _ensure_shared_dir(path)
        return path
    configured = os.environ.get("XDG_STATE_HOME")
    base = Path(configured) if configured else Path.home() / ".local" / "state"
    return base / "vemu" / "experimental3d"


def experimental_config_path() -> Path:
    override = os.environ.get("VEMU_EXPERIMENTAL_CONFIG")
    if override:
        return Path(override).expanduser()
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured else Path.home() / ".config"
    return base / "vemu" / "experimental.json"


def experimental_enabled() -> bool:
    """Return the Settings toggle for experimental features."""
    path = experimental_config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(data.get("enabled", False))


def set_experimental_enabled(enabled: bool) -> Path:
    """Persist the Settings toggle without needing Qt."""
    path = experimental_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"enabled": bool(enabled), "updated_at": _now()}
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return path


def state_path(domain: str, state_dir: str | Path | None = None) -> Path:
    base = Path(state_dir).expanduser() if state_dir else experimental_state_dir()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", domain).strip("-") or "vm"
    return base / f"{safe}.json"


def _load_state(
    domain: str, state_dir: str | Path | None = None
) -> dict[str, Any] | None:
    path = state_path(domain, state_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VemuError(f"Cannot read experimental 3D state: {exc}") from exc
    if not isinstance(data, dict):
        raise VemuError("Invalid experimental 3D state.")
    return data


def _write_state(
    domain: str, data: dict[str, Any], state_dir: str | Path | None = None
) -> Path:
    path = state_path(domain, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o640 if os.geteuid() == 0 else 0o600)
    except OSError:
        pass
    temporary.replace(path)
    if os.geteuid() == 0:
        try:
            import grp

            group = grp.getgrnam("vemu").gr_gid
            os.chown(path, -1, group)
            os.chmod(path, 0o640)
        except (KeyError, OSError):
            pass
    return path


def list_states(
    state_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """List experimental 3D state files without touching any VM.

    The shared directory is readable by the ``vemu`` group. A user-level
    ``--direct`` run may not be able to read it; unreadable directories are
    skipped instead of failing the settings page.
    """
    candidates: list[Path] = []
    if state_dir:
        candidates.append(Path(state_dir).expanduser())
    else:
        shared = Path("/var/lib/vemu/experimental3d")
        if shared.is_dir():
            candidates.append(shared)
        user = experimental_state_dir()
        if user not in candidates:
            candidates.append(user)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for directory in candidates:
        try:
            files = sorted(directory.glob("*.json"))
        except OSError:
            continue
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            domain = str(data.get("domain") or path.stem)
            if domain in seen:
                continue
            seen.add(domain)
            host_changes = data.get("host_changes") or {}
            result.append(
                {
                    "domain": domain,
                    "enabled": bool(data.get("enabled")),
                    "status": data.get("status"),
                    "release": data.get("release"),
                    "console": data.get("console"),
                    "layer": data.get("layer"),
                    "state_path": str(path),
                    "host_security_change": bool(
                        isinstance(host_changes, dict)
                        and host_changes.get("backup")
                    ),
                }
            )
    return sorted(result, key=lambda item: item["domain"].casefold())


def cleanup_deleted(
    domain: str,
    *,
    purge_state: bool = True,
) -> dict[str, Any]:
    """Remove host artifacts after the VM itself has been deleted.

    The experimental 3D helper never restores host-wide qemu.conf changes
    automatically. If a state file recorded such a change, the caller gets a
    warning and must restore it deliberately.
    """
    state = _load_state(domain, None)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", domain).strip("-") or "vm"
    base = state_path(domain, None).parent
    paths: list[Path] = [
        state_path(domain, None),
        base / f"{safe}.domain.xml",
        base / f"{safe}.id_ed25519",
        base / f"{safe}.id_ed25519.pub",
        base / f"{safe}.known_hosts",
    ]
    if state:
        xml_backup = state.get("xml_backup")
        if xml_backup:
            paths.append(Path(str(xml_backup)))
    warnings: list[str] = []
    host_changes = (state or {}).get("host_changes") or {}
    if isinstance(host_changes, dict) and host_changes.get("backup"):
        warnings.append(
            "Host /etc/libvirt/qemu.conf may still have seccomp_sandbox=0 "
            "from experimental 3D. It was NOT restored automatically. "
            f"Backup: {host_changes.get('backup')}"
        )
    removed: list[str] = []
    kept: list[str] = []
    for path in paths:
        if not path.exists():
            continue
        if not purge_state:
            kept.append(str(path))
            continue
        try:
            path.unlink()
            removed.append(str(path))
        except OSError as exc:
            warnings.append(f"Cannot remove {path}: {exc}")
    return {
        "domain": domain,
        "removed": removed,
        "kept": kept,
        "warnings": warnings,
    }


def cache_dir() -> Path:
    override = os.environ.get("VEMU_EXPERIMENTAL_CACHE_DIR")
    if override:
        base = Path(override).expanduser()
    elif os.geteuid() == 0:
        base = Path("/var/cache/vemu/experimental3d")
    else:
        configured = os.environ.get("XDG_CACHE_HOME")
        root = Path(configured) if configured else Path.home() / ".cache"
        base = root / "vemu" / "experimental3d"
    return base / RELEASE


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _download(url: str, target: Path, expected: str) -> Path:
    if target.is_file():
        payload = target.read_bytes()
        if _sha256(payload) == expected.casefold():
            return target
    request = urllib.request.Request(
        url, headers={"User-Agent": "vEMU experimental3d"}
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            payload = response.read()
    except OSError as exc:
        raise VemuError(f"Cannot download {url}: {exc}") from exc
    digest = _sha256(payload)
    if digest != expected.casefold():
        raise VemuError(
            f"Driver checksum mismatch for {target.name}: "
            f"expected {expected}, got {digest}."
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(target)
    return target


def download_driver() -> Path:
    """Download and verify the pinned installer into the host cache."""
    return _download(
        DOWNLOAD_BASE + SETUP_NAME, cache_dir() / SETUP_NAME, SETUP_SHA256
    )


def _run(
    command: list[str], *, timeout: int = 120, check: bool = True
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except OSError as exc:
        raise VemuError(f"Cannot run {' '.join(command)}: {exc}") from exc
    if check and result.returncode != 0:
        raise VemuError(
            "Command failed: "
            + " ".join(command)
            + ": "
            + (result.stderr or result.stdout).strip()
        )
    return result


def _render_node() -> str:
    nodes = sorted(Path("/dev/dri").glob("renderD*"))
    return str(nodes[0]) if nodes else ""


def qemu_conf_text() -> str | None:
    """Read qemu.conf, or return None when the caller cannot read it."""
    try:
        return QEMU_CONF.read_text(encoding="utf-8")
    except OSError:
        return None


def seccomp_value(
    text: object = _UNSET, *, domain: str | None = None
) -> str:
    """Return ``0``/``1``, ``unknown``, or raise when host discovery is blocked.

    The daemon runs as root and reads qemu.conf.  ``--direct`` development
    runs may be unprivileged; for a running domain the QEMU command line is a
    reliable fallback because libvirt passes ``-sandbox off`` when seccomp is
    disabled.
    """
    raw = qemu_conf_text() if text is _UNSET else text
    if isinstance(raw, str):
        for line in raw.splitlines():
            match = re.match(r"^\s*seccomp_sandbox\s*=\s*([^\s#]+)", line)
            if match:
                value = match.group(1).strip().strip('"').casefold()
                if value in OFF_VALUES:
                    return "0"
                if value in ON_VALUES:
                    return "1"
        return "1"
    if domain:
        probed = _sandbox_from_qemu_process(domain)
        if probed is not None:
            return probed
    return "unknown"


def _sandbox_from_qemu_process(domain: str) -> str | None:
    needle = f"guest={domain}"
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            data = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        parts = [
            item.decode("utf-8", "replace")
            for item in data.split(b"\0")
            if item
        ]
        if not any(
            part == needle or part.startswith(needle + ",")
            for part in parts
        ):
            continue
        for index, part in enumerate(parts):
            if part != "-sandbox" or index + 1 >= len(parts):
                continue
            value = parts[index + 1].casefold()
            if value.startswith("off"):
                return "0"
            if value.startswith("on"):
                return "1"
        return None
    return None


def _virgl_render_server() -> str:
    for candidate in (
        "/usr/libexec/virgl_render_server",
        "/usr/lib/virgl_render_server",
        "/usr/lib64/virgl_render_server",
    ):
        if Path(candidate).is_file():
            return candidate
    found = shutil.which("virgl_render_server")
    return found or ""


def _qemu_user_render_group() -> bool:
    try:
        import grp
        import pwd

        pwd.getpwnam("qemu")
        group = grp.getgrnam("render").gr_gid
        return group in os.getgrouplist("qemu", 0)
    except (KeyError, ImportError, OSError):
        return False


def host_report(domain: str | None = None) -> dict[str, Any]:
    qemu = shutil.which("qemu-system-x86_64") or shutil.which("qemu-kvm")
    render = _render_node()
    probe = ""
    props = {"blob": False, "hostmem": False, "venus": False}
    if qemu:
        result = _run(
            [qemu, "-device", "virtio-vga-gl,help"],
            timeout=20,
            check=False,
        )
        probe = (result.stdout or result.stderr).strip()
        for name in props:
            props[name] = f"{name}=" in probe
    vendor = ""
    try:
        vendor = Path(
            f"/sys/class/dri/{Path(render).name}/device/vendor"
        ).read_text(encoding="utf-8").strip()
    except OSError:
        try:
            vendor = Path(
                f"/sys/class/drm/{Path(render).name}/device/vendor"
            ).read_text(encoding="utf-8").strip()
        except OSError:
            vendor = ""
    return {
        "qemu": qemu or "",
        "virtio_vga_gl": bool(qemu and "virtio-vga-gl" in probe),
        "virtio_vga_gl_props": props,
        "render_node": render,
        "virgl_render_server": _virgl_render_server(),
        "gpu_vendor": vendor,
        "qemu_in_render_group": _qemu_user_render_group(),
        "remote_viewer": find_spiceviewer() or "",
        "seccomp_sandbox": seccomp_value(domain=domain),
        "release": RELEASE,
        "driver_version": DRIVER_VERSION,
        "setup_sha256": SETUP_SHA256,
    }


def _as_root_prefix() -> list[str]:
    if os.geteuid() == 0:
        return []
    return ["sudo", "-n"]


def ensure_virt_viewer(*, assume_yes: bool = False) -> str:
    """Return remote-viewer, installing virt-viewer when confirmed."""
    viewer = find_spiceviewer()
    if viewer:
        return viewer
    if not assume_yes:
        raise VemuError(
            "remote-viewer is missing. " + viewer_install_hint()
        )
    prefix = _as_root_prefix()
    candidates = (
        ("dnf", ["dnf", "install", "-y", "virt-viewer"]),
        ("zypper", ["zypper", "--non-interactive", "install", "virt-viewer"]),
        ("apt-get", ["apt-get", "install", "-y", "virt-viewer"]),
        ("pacman", ["pacman", "-S", "--noconfirm", "virt-viewer"]),
    )
    for binary, command in candidates:
        if shutil.which(binary) is None:
            continue
        result = _run([*prefix, *command], timeout=900, check=False)
        viewer = find_spiceviewer()
        if viewer:
            return viewer
        if result.returncode != 0:
            continue
    raise VemuError(
        "Cannot install virt-viewer automatically. " + viewer_install_hint()
    )


def _active_libvirt_unit() -> str:
    for unit in ("virtqemud", "libvirtd"):
        result = _run(
            ["systemctl", "is-active", unit], timeout=15, check=False
        )
        if result.stdout.strip() == "active":
            return unit
    return "libvirtd"


def _restart_libvirt() -> str:
    if shutil.which("systemctl") is None:
        raise VemuError("Cannot restart libvirt without systemctl.")
    unit = _active_libvirt_unit()
    _run(["systemctl", "restart", unit], timeout=180)
    return unit


def ensure_seccomp(
    *,
    current: str,
    accept: bool,
    uri: str = "qemu:///system",
    domain: str | None = None,
) -> dict[str, Any]:
    """Disable the host-wide QEMU seccomp sandbox after explicit consent."""
    if current in OFF_VALUES:
        return {"changed": False, "before": current, "after": current}
    if current == "unknown":
        raise VemuError(
            "Cannot read /etc/libvirt/qemu.conf to check seccomp_sandbox. "
            "Run the helper through vemud (default), or check the setting "
            "manually."
        )
    if not accept:
        raise VemuError(
            "vgpu3d needs seccomp_sandbox = 0 in /etc/libvirt/qemu.conf. "
            "This is host-wide and weakens QEMU for every VM. Rerun with "
            "--accept-host-security-change after reviewing the warning."
        )
    with LibvirtBackend(uri) as backend:
        running = [
            item.name
            for item in backend.list_domains()
            if item.state == "running" and item.name != domain
        ]
    if running:
        raise VemuError(
            "Changing qemu.conf restarts libvirt and would disrupt the "
            "running VMs: "
            + ", ".join(sorted(running))
            + ". Shut them down first."
        )
    text = qemu_conf_text()
    if text is None:
        raise VemuError(f"Cannot read {QEMU_CONF}.")
    backup = QEMU_CONF.with_suffix(QEMU_CONF.suffix + ".vemu-3d-backup")
    if not backup.exists():
        backup.write_text(text, encoding="utf-8")
    lines = text.splitlines()
    changed = False
    for index, line in enumerate(lines):
        if re.match(r"^\s*seccomp_sandbox\s*=", line):
            lines[index] = "seccomp_sandbox = 0"
            changed = True
            break
    if not changed:
        lines.append("seccomp_sandbox = 0")
    QEMU_CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")
    unit = _restart_libvirt()
    return {
        "changed": True,
        "before": current,
        "after": "0",
        "backup": str(backup),
        "unit": unit,
    }


def restore_seccomp(backup: str | Path | None) -> dict[str, Any]:
    if not backup:
        return {"changed": False}
    path = Path(str(backup))
    if not path.is_file():
        raise VemuError(f"qemu.conf backup is missing: {path}")
    shutil.copy2(path, QEMU_CONF)
    unit = _restart_libvirt()
    return {"changed": True, "restored": str(path), "unit": unit}


def apply_xml(xml: str) -> str:
    """Apply the validated vgpu3d XML deltas to a libvirt domain XML."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise ValidationError(f"Invalid domain XML: {exc}") from exc
    devices = root.find("devices")
    if devices is None:
        raise ValidationError("Domain XML has no devices section.")

    for old in list(root.findall("memoryBacking")):
        root.remove(old)
    memory = ET.Element("memoryBacking")
    ET.SubElement(memory, "source", type="memfd")
    ET.SubElement(memory, "access", mode="shared")
    anchor = root.find("vcpu")
    if anchor is None:
        anchor = devices
    root.insert(list(root).index(anchor), memory)

    for graphics in list(devices.findall("graphics")):
        devices.remove(graphics)
    spice = ET.SubElement(
        devices, "graphics", type="spice", autoport="yes"
    )
    ET.SubElement(spice, "listen", type="address", address="127.0.0.1")
    egl = ET.SubElement(devices, "graphics", type="egl-headless")
    ET.SubElement(egl, "gl", rendernode=_render_node() or "/dev/dri/renderD128")

    for video in list(devices.findall("video")):
        devices.remove(video)
    video = ET.SubElement(devices, "video")
    model = ET.SubElement(
        video,
        "model",
        type="virtio",
        heads="1",
        primary="yes",
        device="virtio-vga-gl",
    )
    ET.SubElement(model, "acceleration", accel3d="yes")

    for old in list(root.findall(f"{{{QEMU_NS}}}commandline")):
        root.remove(old)
    commandline = ET.SubElement(root, f"{{{QEMU_NS}}}commandline")
    for value in (
        ("-global", "virtio-vga-gl.blob=true"),
        ("-global", "virtio-vga-gl.hostmem=4G"),
        ("-global", "virtio-vga-gl.venus=true"),
    ):
        ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=value[0])
        ET.SubElement(commandline, f"{{{QEMU_NS}}}arg", value=value[1])
    return ET.tostring(root, encoding="unicode")


def secure_boot_enabled(xml: str) -> bool:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    enrolled = None
    for feature in root.findall("os/firmware/feature"):
        if feature.get("name") == "enrolled-keys":
            enrolled = str(feature.get("enabled", "")).casefold()
    if enrolled is not None:
        return enrolled in ON_VALUES
    loader = root.find("os/loader")
    return bool(
        loader is not None
        and str(loader.get("secure", "")).casefold() in ON_VALUES
    )


def _wait_port(host: str, port: int, timeout: float = 180.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2):
                return True
        except OSError:
            time.sleep(2)
    return False


def _wait_qga(
    backend: LibvirtBackend, domain: str, timeout: float = 240
) -> None:
    transport = QGATransport(backend, domain)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if transport.ping():
            return
        time.sleep(2)
    raise VemuError("QEMU guest agent did not respond.")


def _guest_host(backend: LibvirtBackend, domain: str) -> str:
    from . import service

    try:
        return service._seamless_guest_ip(backend, domain)
    except Exception as exc:  # noqa: BLE001 - service raises several types
        raise VemuError(f"Cannot determine the guest IP: {exc}") from exc


def _stop_vm(
    backend: LibvirtBackend, domain: str, *, force: bool = False
) -> None:
    info = backend.info(domain)
    if info.state == "running":
        try:
            backend.shutdown(domain, timeout=180)
        except VemuError:
            if not force:
                raise
            backend.destroy(domain)
    if backend.info(domain).state != "stopped":
        raise Conflict(f"Domain '{domain}' did not shut down.")


def _query_windows_user(
    transport: QGATransport, username: str
) -> dict[str, Any]:
    safe = username.replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
$user = Get-LocalUser -Name '{safe}' -ErrorAction Stop
$adminGroup = Get-LocalGroup |
  Where-Object {{ $_.SID.Value -eq 'S-1-5-32-544' }}
$isAdmin = $false
if ($null -ne $adminGroup) {{
  $isAdmin = @(Get-LocalGroupMember -Group $adminGroup.Name `
    -ErrorAction SilentlyContinue |
    Where-Object {{ $_.SID.Value -eq $user.SID.Value }}).Count -gt 0
}}
$profile = Get-CimInstance Win32_UserProfile -ErrorAction SilentlyContinue |
  Where-Object {{ $_.SID -eq $user.SID.Value }} | Select-Object -First 1
[pscustomobject]@{{
  user = $user.Name
  admin = $isAdmin
  profile = if ($null -ne $profile) {{ [string]$profile.LocalPath }} else {{ '' }}
}} | ConvertTo-Json -Compress
"""
    result = transport.run_powershell(script, timeout=45)
    if not result.ok:
        raise VemuError(
            f"Cannot inspect Windows user '{username}': {result.output}"
        )
    try:
        data = json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise VemuError(
            f"Cannot parse the Windows user check: {result.output}"
        ) from exc
    return data if isinstance(data, dict) else {}


def _query_service(transport: QGATransport, name: str) -> str:
    script = (
        f"$s = Get-Service -Name '{name}' -ErrorAction SilentlyContinue; "
        "if ($null -eq $s) { 'missing' } else { [string]$s.Status }"
    )
    result = transport.run_powershell(script, timeout=30)
    return result.stdout.strip() or result.stderr.strip() or "unknown"


def _prepare_ssh(
    backend: LibvirtBackend,
    domain: str,
    username: str,
    *,
    state_dir: str | Path | None,
) -> SSHTransport:
    qga = QGATransport(backend, domain)
    if not qga.ping():
        raise VemuError("QEMU guest agent is not running.")
    qemu_ga = _query_service(qga, "QEMU-GA")
    sshd = _query_service(qga, "sshd")
    if sshd.casefold() != "running":
        raise VemuError(
            "OpenSSH Server (sshd) is not running in the guest. Install it, "
            "enable the service and open TCP 22 before enabling 3D."
        )
    if qemu_ga.casefold() != "running":
        raise VemuError("QEMU guest agent (QEMU-GA) is not running.")
    user = _query_windows_user(qga, username)
    if not user.get("profile") and not user.get("admin"):
        raise VemuError(
            f"Windows profile for '{username}' does not exist. Log in once "
            "as that user before enabling 3D."
        )
    base = (
        Path(state_dir).expanduser()
        if state_dir
        else experimental_state_dir()
    )
    base.mkdir(parents=True, exist_ok=True)
    key = base / f"{domain}.id_ed25519"
    public = ensure_keypair(key)
    public_key = public.read_text(encoding="utf-8").strip()
    if user.get("admin"):
        install_admin_key(qga, public_key)
    else:
        install_user_key(qga, username, public_key)
    host = _guest_host(backend, domain)
    if not _wait_port(host, 22, timeout=120):
        raise VemuError(
            f"SSH is not listening on {host}:22. Check the guest firewall "
            "and the OpenSSH Server service."
        )
    known = base / f"{domain}.known_hosts"
    scan_host_key(host, known)
    ssh = SSHTransport(host, username, key, known, connect_timeout=30)
    deadline = time.monotonic() + 300
    last = ""
    while time.monotonic() < deadline:
        try:
            result = ssh.run("echo ok", timeout=30)
        except VemuError as exc:
            last = str(exc)
            time.sleep(3)
            continue
        if result.stdout.strip() == "ok":
            return ssh
        last = result.stderr or result.stdout
        time.sleep(3)
    raise VemuError(f"SSH key login did not become ready: {last}")


def _wait_ssh(ssh: SSHTransport, timeout: float = 360) -> None:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            if ssh.ping():
                return
        except VemuError as exc:
            last = str(exc)
        time.sleep(3)
    raise VemuError(
        "SSH did not come back after the guest reboot"
        + (f": {last}" if last else ".")
    )


def _guest_boot_time(transport: Any) -> str:
    result = transport.run_powershell(
        "(Get-CimInstance Win32_OperatingSystem)."
        "LastBootUpTime.ToString('o')",
        timeout=30,
    )
    return result.stdout.strip() if result.ok else ""


def _wait_rebooted(
    backend: LibvirtBackend, domain: str, before: str
) -> None:
    if not before:
        # Without a boot timestamp, require the agent to drop before waiting
        # for it to return; otherwise an agent that never went down would be
        # mistaken for a finished reboot.
        saw_down = False
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                if not QGATransport(backend, domain).ping():
                    saw_down = True
                    break
            except VemuError:
                saw_down = True
                break
            time.sleep(3)
        if not saw_down:
            raise VemuError(
                "The guest agent never dropped; cannot verify the reboot."
            )
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        time.sleep(5)
        try:
            transport = QGATransport(backend, domain)
            if not transport.ping():
                continue
            after = _guest_boot_time(transport)
            if before and after and after != before:
                return
            if not before and after:
                return
        except VemuError:
            continue
    raise VemuError("The guest did not reboot in time.")


def _reboot_guest(
    ssh: SSHTransport, backend: LibvirtBackend, domain: str
) -> None:
    before = ""
    try:
        before = _guest_boot_time(QGATransport(backend, domain))
    except VemuError:
        pass
    if not before:
        try:
            before = _guest_boot_time(ssh)
        except VemuError:
            pass
    # QGA reboots without holding an SSH channel open across the shutdown.
    # SSH fallback is kept for guests where the agent refuses the command.
    try:
        backend.qemu_agent_command(
            domain,
            {"execute": "guest-shutdown", "arguments": {"mode": "reboot"}},
            timeout=30,
        )
    except VemuError:
        try:
            ssh.run("shutdown /r /t 5 /f", timeout=30)
        except VemuError:
            # The disconnect can race the command completion; the boot-time
            # check below is the source of truth.
            pass
    _wait_rebooted(backend, domain, before)
    _wait_ssh(ssh)


INSTALL_SCRIPT = "C:/Program Files/vgpu3d/install.ps1"
INSTALL_DIR = "C:/Program Files/vgpu3d"


def _patch_install_script(ssh: SSHTransport) -> bool:
    """Replace Import-Certificate, which fails with E_ACCESSDENIED over SSH.

    The pinned installer extracts ``install.ps1`` before failing.  Importing
    the test certificate into LocalMachine works from an interactive desktop
    but not from a non-interactive OpenSSH/QGA session.  ``certutil -addstore``
    is the upstream-documented equivalent and works in this context.
    """
    try:
        data = ssh.download(INSTALL_SCRIPT, max_bytes=2 * 1024 * 1024)
    except VemuError:
        return False
    text = data.decode("utf-8-sig", "replace")
    replacements = {
        "Import-Certificate -FilePath $cer "
        "-CertStoreLocation Cert:\\LocalMachine\\Root | Out-Null":
            "& certutil.exe -addstore -f Root $cer | Out-Null",
        "Import-Certificate -FilePath $cer "
        "-CertStoreLocation Cert:\\LocalMachine\\TrustedPublisher | Out-Null":
            "& certutil.exe -addstore -f TrustedPublisher $cer | Out-Null",
    }
    changed = False
    for old, new in replacements.items():
        if old in text:
            text = text.replace(old, new)
            changed = True
    if not changed:
        return False
    ssh.upload(text.encode("utf-8"), INSTALL_SCRIPT)
    return True


def _run_install_script(ssh: SSHTransport) -> int:
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    command = (
        f'"{powershell}" -NoProfile -ExecutionPolicy Bypass '
        f'-File "{INSTALL_SCRIPT}" -PackageDir "{INSTALL_DIR}" '
        "& echo EXIT=%ERRORLEVEL%"
    )
    result = ssh.run(command, timeout=1200)
    match = re.search(r"EXIT=(\d+)", result.stdout)
    if match is None:
        raise VemuError(
            "The patched vgpu3d install.ps1 did not report an exit code: "
            + (result.output or "no output")
        )
    return int(match.group(1))


def _install_driver(ssh: SSHTransport, installer: Path) -> int:
    remote_dir = "C:/ProgramData/vEMU"
    made = ssh.run_powershell(
        f"New-Item -ItemType Directory -Path '{remote_dir}' -Force | Out-Null",
        timeout=30,
    )
    if not made.ok:
        raise VemuError(f"Cannot create {remote_dir}: {made.output}")
    remote = f"{remote_dir}/{SETUP_NAME}"
    ssh.upload(installer.read_bytes(), remote)
    script = (
        f"$p = Start-Process -FilePath '{remote}' -ArgumentList '/S' "
        "-Wait -PassThru; Write-Output ('exit=' + $p.ExitCode)"
    )
    result = ssh.run_powershell(script, timeout=1200)
    match = re.search(r"exit=(\d+)", result.stdout)
    if not result.ok or match is None:
        raise VemuError(
            "vgpu3d installer failed: " + (result.output or "no output")
        )
    code = int(match.group(1))
    if code in (0, 3010):
        return code
    # Known upstream issue: Import-Certificate returns E_ACCESSDENIED when
    # setup is started from SSH/QGA instead of an interactive desktop.  The
    # installer has already extracted its scripts; patch and rerun them.
    if not _patch_install_script(ssh):
        raise VemuError(
            f"vgpu3d installer exited with code {code} and its install.ps1 "
            "could not be patched for the certificate workaround."
        )
    patched = _run_install_script(ssh)
    if patched == 3:
        return 3010
    if patched not in (0, 3010):
        raise VemuError(
            f"vgpu3d install.ps1 exited with code {patched} after the "
            "certutil workaround."
        )
    return patched


def _parse_adapters(text: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(text.strip() or "[]")
    except json.JSONDecodeError as exc:
        raise VemuError(
            "Cannot parse the Windows display adapter: " + text
        ) from exc
    return data if isinstance(data, list) else [data]


ADAPTER_QUERY = (
    "Get-CimInstance Win32_VideoController | "
    "Select-Object Name,DriverVersion,Status,ConfigManagerErrorCode | "
    "ConvertTo-Json -Compress"
)


def _query_adapters(transport: Any) -> list[dict[str, Any]]:
    result = transport.run_powershell(ADAPTER_QUERY, timeout=60)
    if not result.ok:
        raise VemuError(
            "Cannot query the Windows display adapter: " + result.output
        )
    return _parse_adapters(result.stdout)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class DriverNotReady(VemuError):
    """The adapter exists but Windows reports a problem code."""

    def __init__(
        self,
        message: str,
        *,
        problem_code: int | None = None,
        adapter: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.problem_code = problem_code
        self.adapter = adapter or {}


def _verify_driver(ssh: SSHTransport) -> dict[str, Any]:
    adapters = _query_adapters(ssh)
    for adapter in adapters:
        if not isinstance(adapter, dict):
            continue
        name = str(adapter.get("Name") or "")
        version = str(adapter.get("DriverVersion") or "")
        if "vgpu" not in name.casefold():
            continue
        status = str(adapter.get("Status") or "").strip()
        code = _int_or_none(adapter.get("ConfigManagerErrorCode"))
        if status.casefold() not in {"", "ok"} or code not in (None, 0):
            raise DriverNotReady(
                "The vgpu3d adapter is not ready "
                f"(Status={status or 'unknown'}, code={code}). "
                "This is the known VgpuBootGuard latch; the helper will "
                "reset it and restart the device.",
                problem_code=code,
                adapter=adapter,
            )
        return {
            "adapter": name,
            "version": version,
            "expected_version": DRIVER_VERSION,
            "matches_pin": version == DRIVER_VERSION,
            "status": status or "OK",
            "problem_code": 0 if code is None else code,
            "adapters": adapters,
        }
    raise VemuError(
        "The vgpu3d display adapter did not appear after installation. "
        "Adapters: "
        + json.dumps(adapters, ensure_ascii=False)
        + ". Check test signing, Secure Boot and the installer log."
    )


def _repair_boot_guard(ssh: SSHTransport) -> dict[str, Any]:
    """Reset the known VgpuBootGuard latch and restart the adapter.

    Rapid reboots during installation can leave the driver at Code 37
    (CM_PROB_FAILED_DRIVER_ENTRY).  The upstream recovery is to set
    ``HKLM\\SYSTEM\\CurrentControlSet\\Services\\VioGpu3D\\VgpuBootGuard``
    to 0 and restart the PnP device.
    """
    script = r"""
$ErrorActionPreference = 'Stop'
$svc = 'HKLM:\SYSTEM\CurrentControlSet\Services\VioGpu3D'
if (-not (Test-Path $svc)) { throw 'VioGpu3D service key not found' }
Set-ItemProperty -Path $svc -Name VgpuBootGuard -Value 0 -Type DWord
$dev = Get-PnpDevice -Class Display -PresentOnly |
  Where-Object { $_.InstanceId -like 'PCI\VEN_1AF4&DEV_1050*' } |
  Select-Object -First 1
if ($null -eq $dev) { throw 'vgpu3d display device not found' }
pnputil /restart-device "$($dev.InstanceId)" | Out-String | Write-Output
Start-Sleep -Seconds 8
Get-ItemProperty -Path $svc -Name VgpuBootGuard |
  Select-Object VgpuBootGuard | ConvertTo-Json -Compress
"""
    result = ssh.run_powershell(script, timeout=180)
    if not result.ok:
        raise VemuError(
            "Cannot reset the vgpu3d VgpuBootGuard latch: " + result.output
        )
    guard = None
    for line in reversed(result.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            guard = payload.get("VgpuBootGuard")
            break
    return {
        "repaired": True,
        "boot_guard": guard,
        "output": result.stdout.strip(),
    }


def _snapshot_present(
    backend: LibvirtBackend, domain: str, layer: str
) -> bool:
    if not layer:
        return False
    info = backend.info(domain)
    if not info.disks:
        return False
    chain_dir = Path(info.disks[0]).parent
    if not (chain_dir / "chain.json").is_file():
        return False
    try:
        payload = ChainManager(chain_dir).info()
    except VemuError:
        return False
    return any(
        str(item.get("id") or "") == layer
        and not item.get("active")
        for item in payload.get("layers") or []
        if isinstance(item, dict)
    )


def _guest_adapters(backend: LibvirtBackend, domain: str) -> list[dict[str, Any]]:
    try:
        if backend.info(domain).state != "running":
            return []
        transport = QGATransport(backend, domain)
        if not transport.ping():
            return []
        return _query_adapters(transport)
    except Exception:  # noqa: BLE001 - status is best effort
        return []


def status(
    domain: str,
    *,
    state_dir: str | Path | None = None,
    seamless_state_dir: str | Path | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Return the experimental 3D state without changing anything."""
    from . import service

    state = _load_state(domain, state_dir)
    with LibvirtBackend(uri) as backend:
        info = backend.info(domain)
        xml = backend.config_xml(domain)
        try:
            root = ET.fromstring(xml)
            graphics = [
                item.get("type") for item in root.findall("devices/graphics")
            ]
            video = [
                item.get("type")
                for item in root.findall("devices/video/model")
            ]
        except ET.ParseError:
            graphics, video = [], []
        layer = str((state or {}).get("layer") or "")
        snapshot = _snapshot_present(backend, domain, layer)
        adapters = _guest_adapters(backend, domain)
    seamless = service._load_seamless_state(domain, seamless_state_dir)
    warnings: list[str] = []
    if state and not snapshot:
        warnings.append(
            "The experimental-3d checkpoint is missing; automatic revert "
            "is unavailable."
        )
    if not state and "spice" in graphics and "egl-headless" in graphics:
        warnings.append(
            "SPICE/EGL-headless is configured but no experimental 3D state "
            "was found. The VM may have been converted manually."
        )
    for adapter in adapters:
        if not isinstance(adapter, dict):
            continue
        if "vgpu" not in str(adapter.get("Name") or "").casefold():
            continue
        status = str(adapter.get("Status") or "").strip()
        code = _int_or_none(adapter.get("ConfigManagerErrorCode"))
        if status.casefold() not in {"", "ok"} or code not in (None, 0):
            warnings.append(
                "The vgpu3d adapter is not ready "
                f"(Status={status or 'unknown'}, code={code}). This is the "
                "known VgpuBootGuard latch; run "
                f"'vemu experimental 3d repair {domain}' to reset it."
            )
    if state and "spice" not in graphics:
        warnings.append("The VM no longer uses the SPICE console.")
    if seamless and seamless.get("active", True):
        warnings.append(
            "Seamless mode is active; enabling 3D will pause it."
        )
    return {
        "domain": domain,
        "enabled": bool(state and state.get("enabled")),
        "state": state,
        "snapshot_present": snapshot,
        "running": info.state == "running",
        "graphics": graphics,
        "video": video,
        "adapters": adapters,
        "seamless": {
            "prepared": bool(seamless),
            "active": bool(seamless.get("active", True))
            if seamless
            else False,
        },
        "warnings": warnings,
        "host": host_report(domain=domain),
    }


def _set_seccomp_default() -> dict[str, Any]:
    """Set ``seccomp_sandbox = 1`` when the original backup is unavailable."""
    try:
        text = QEMU_CONF.read_text(encoding="utf-8")
    except OSError as exc:
        raise VemuError(f"Cannot read {QEMU_CONF}: {exc}") from exc
    lines = text.splitlines()
    changed = False
    for index, line in enumerate(lines):
        if re.match(r"^\s*seccomp_sandbox\s*=", line):
            lines[index] = "seccomp_sandbox = 1"
            changed = True
            break
    if not changed:
        lines.append("seccomp_sandbox = 1")
    QEMU_CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")
    unit = _restart_libvirt()
    return {
        "changed": True,
        "before": "0",
        "after": "1",
        "backup": "",
        "unit": unit,
        "fallback": True,
    }


def restore_host_security(
    *,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Restore the pre-3D qemu.conf backup when no 3D VM needs it.

    This is intentionally refused while any enabled experimental 3D state
    exists or while a VM is running, because restoring qemu.conf restarts
    libvirt. Disabled/stale state files are updated to forget the host change.
    """
    states = list_states()
    enabled = [item for item in states if item.get("enabled")]
    if enabled:
        raise Conflict(
            "Experimental 3D is still enabled for: "
            + ", ".join(str(item.get("domain")) for item in enabled)
            + ". Revert or delete those VMs before restoring host security."
        )
    current = seccomp_value()
    if current == "unknown":
        raise VemuError(
            "Cannot read /etc/libvirt/qemu.conf; restore host security "
            "manually."
        )
    if current not in OFF_VALUES:
        return {"changed": False, "seccomp_sandbox": current}
    backup = QEMU_CONF_BACKUP
    with LibvirtBackend(uri) as backend:
        running = [
            item.name
            for item in backend.list_domains()
            if item.state == "running"
        ]
    if running:
        raise Conflict(
            "Shut down all VMs before restoring host security; libvirt must "
            "restart. Running: " + ", ".join(sorted(running))
        )
    if backup.is_file():
        restored = restore_seccomp(str(backup))
    else:
        restored = _set_seccomp_default()
    updated: list[str] = []
    for item in states:
        path = Path(str(item.get("state_path") or ""))
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        changes = data.get("host_changes")
        if not isinstance(changes, dict) or not changes.get("backup"):
            continue
        data["host_changes"] = {}
        data["host_security_restored_at"] = _now()
        try:
            _write_state(
                str(data.get("domain") or item.get("domain")),
                data,
                state_dir=str(path.parent),
            )
        except OSError:
            continue
        updated.append(str(item.get("domain") or ""))
    return {
        **restored,
        "seccomp_sandbox": "1",
        "states_updated": [name for name in updated if name],
    }


def repair(
    domain: str,
    *,
    state_dir: str | Path | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Recover a latched VgpuBootGuard without re-running the conversion."""
    state = _load_state(domain, state_dir)
    if not state:
        raise NotFound(f"No experimental 3D state for '{domain}'.")
    with LibvirtBackend(uri) as backend:
        if backend.info(domain).state != "running":
            raise Conflict(
                f"Start '{domain}' before repairing the 3D adapter."
            )
        qga = QGATransport(backend, domain)
        if not qga.ping():
            raise VemuError("QEMU guest agent is not responding.")
        try:
            verified = _verify_driver(qga)
        except DriverNotReady as exc:
            if exc.problem_code not in (None, 37):
                raise
            repair_result = _repair_boot_guard(qga)
            verified = _verify_driver(qga)
            result = {
                "domain": domain,
                "status": "repaired",
                "problem_code": exc.problem_code,
                "repair": repair_result,
                "driver": verified,
            }
        else:
            result = {
                "domain": domain,
                "status": "already-ok",
                "driver": verified,
            }
    state["driver_verify"] = verified
    if result["status"] == "repaired":
        state["driver_repair"] = {
            **result["repair"],
            "at": _now(),
        }
    _write_state(domain, state, state_dir)
    return result


def _revert_state(
    domain: str,
    state: dict[str, Any],
    *,
    uri: str,
    state_dir: str | Path | None,
) -> dict[str, Any]:
    from . import service

    with LibvirtBackend(uri) as backend:
        _stop_vm(backend, domain, force=True)
    layer = str(state.get("layer") or "")
    if layer:
        service.snapshots_revert(domain, layer, uri=uri)
    xml_backup = state.get("xml_backup")
    if xml_backup and Path(str(xml_backup)).is_file():
        with LibvirtBackend(uri) as backend:
            backend.redefine_domain(
                domain,
                Path(str(xml_backup)).read_text(encoding="utf-8"),
            )
    else:
        raise VemuError(
            "The pre-3D XML backup is missing; the disk was reverted but "
            "the console configuration was not. Restore the XML backup "
            "manually before starting the VM."
        )
    host_changes = state.get("host_changes") or {}
    restored = {}
    if isinstance(host_changes, dict) and host_changes.get("backup"):
        restored = restore_seccomp(host_changes.get("backup"))
    restored_seamless = False
    if state.get("seamless_paused"):
        seamless_dir = state.get("seamless_state_dir") or None
        saved = service._load_seamless_state(domain, seamless_dir)
        if saved is not None:
            # vm_seamless_deactivate clears the published app list. Put it
            # back so `vemu seamless prepare` can republish the same programs
            # after the 3D revert. The state stays inactive until the user
            # explicitly activates it again.
            saved["apps"] = state.get("seamless_apps") or []
            saved["active"] = False
            saved["deactivated_at"] = saved.get("deactivated_at") or _now()
            service.write_seamless_state(
                service._seamless_state_path(domain, seamless_dir), saved
            )
            restored_seamless = True
    state_path(domain, state_dir).unlink(missing_ok=True)
    base_path = state_path(domain, state_dir)
    base_dir = base_path.parent
    safe = base_path.stem
    for suffix in (".domain.xml", ".id_ed25519", ".id_ed25519.pub",
                   ".known_hosts"):
        try:
            (base_dir / f"{safe}{suffix}").unlink(missing_ok=True)
        except OSError:
            pass
    return {
        "domain": domain,
        "reverted": True,
        "seccomp": restored,
        "snapshot_layer": layer,
        "seamless_apps_restored": restored_seamless,
    }


def revert(
    domain: str,
    *,
    state_dir: str | Path | None = None,
    uri: str = "qemu:///system",
) -> dict[str, Any]:
    """Stop the VM, restore the disk checkpoint and the original XML."""
    state = _load_state(domain, state_dir)
    if not state:
        raise NotFound(f"No experimental 3D state for '{domain}'.")
    result = _revert_state(
        domain, state, uri=uri, state_dir=state_dir
    )
    result["seamless_paused"] = bool(state.get("seamless_paused"))
    if result["seamless_paused"]:
        result["note"] = (
            "Seamless mode was paused. Activate it again manually with "
            "'vemu seamless prepare' or from the desktop app."
        )
    return result


def _precheck(
    backend: LibvirtBackend, domain: str
) -> tuple[Any, str, bool]:
    from . import service

    info = service._require_writable(backend, domain, full=True)
    xml = backend.config_xml(domain)
    if secure_boot_enabled(xml):
        raise VemuError(
            "Secure Boot is enforced. Disable it in the firmware/OVMF "
            "settings before enabling vgpu3d; the test-signed driver cannot "
            "load with Secure Boot on."
        )
    return info, xml, False


def enable(
    domain: str,
    username: str,
    *,
    state_dir: str | Path | None = None,
    seamless_state_dir: str | Path | None = None,
    seamless_shortcut_dir: str | Path | None = None,
    uri: str = "qemu:///system",
    accept_host_security_change: bool = False,
    assume_yes: bool = False,
    dry_run: bool = False,
    keep_broken: bool = False,
) -> dict[str, Any]:
    """Convert a VM to the pinned vgpu3d console profile.

    The caller (CLI or desktop) is responsible for the experimental-feature
    gate.  This function never runs a GUI wizard and never publishes
    RemoteApp programs.
    """
    from . import service

    username = username.strip()
    if not username:
        raise ValidationError("A Windows username is required.")
    report = host_report(domain=domain)
    if not report["virtio_vga_gl"]:
        raise VemuError("This QEMU build has no virtio-vga-gl device.")
    missing = [
        name
        for name, present in report["virtio_vga_gl_props"].items()
        if not present
    ]
    if missing:
        raise VemuError(
            "QEMU's virtio-vga-gl is missing the required properties: "
            + ", ".join(missing)
        )
    if not report["render_node"]:
        raise VemuError("No DRM render node (/dev/dri/renderD*) is available.")
    if not report["virgl_render_server"]:
        raise VemuError(
            "virgl_render_server was not found. Install virglrenderer with "
            "Venus support."
        )

    current = _load_state(domain, state_dir)
    if current and current.get("enabled"):
        return {
            "domain": domain,
            "status": "already-enabled",
            "state": current,
        }

    with LibvirtBackend(uri) as backend:
        info, xml, _ = _precheck(backend, domain)
    if dry_run:
        from . import service as _service

        seamless = _service._load_seamless_state(domain, seamless_state_dir)
        return {
            "domain": domain,
            "dry_run": True,
            "state": info.state,
            "xml": apply_xml(xml),
            "host": report,
            "driver": SETUP_NAME,
            "seccomp_change": report["seccomp_sandbox"] not in OFF_VALUES,
            "seamless_pause": bool(
                seamless and seamless.get("active", True)
            ),
        }

    viewer = ensure_virt_viewer(assume_yes=assume_yes)
    installer = download_driver()
    seamless = service._load_seamless_state(domain, seamless_state_dir)
    paused = False
    paused_apps: list[dict[str, Any]] = []
    if seamless and seamless.get("active", True):
        paused_apps = [
            item
            for item in seamless.get("apps") or []
            if isinstance(item, dict)
        ]
        service.vm_seamless_deactivate(
            domain,
            state_dir=seamless_state_dir,
            shortcut_dir=seamless_shortcut_dir,
        )
        paused = True

    with LibvirtBackend(uri) as backend:
        if backend.info(domain).state == "running":
            _stop_vm(backend, domain)

    base = (
        Path(state_dir).expanduser()
        if state_dir
        else experimental_state_dir()
    )
    base.mkdir(parents=True, exist_ok=True)
    xml_path = base / f"{domain}.domain.xml"
    xml_path.write_text(xml, encoding="utf-8")
    try:
        os.chmod(xml_path, 0o600)
    except OSError:
        pass

    snapshot = service.snapshots_create(
        domain,
        label=SNAPSHOT_LABEL,
        description="v-EMU experimental vgpu3d checkpoint",
        uri=uri,
    )
    layer = str((snapshot.get("layer") or {}).get("id") or "")

    state: dict[str, Any] = {
        "version": STATE_VERSION,
        "domain": domain,
        "enabled": True,
        "status": "prepared",
        "release": RELEASE,
        "driver_version": DRIVER_VERSION,
        "setup_sha256": SETUP_SHA256,
        "layer": layer,
        "snapshot_label": SNAPSHOT_LABEL,
        "xml_backup": str(xml_path),
        "xml_sha256": hashlib.sha256(xml.encode("utf-8")).hexdigest(),
        "host_changes": {},
        "seamless_paused": paused,
        "seamless_state_dir": str(seamless_state_dir or ""),
        "seamless_apps": paused_apps,
        "seamless_profile": (seamless or {}).get("profile") if paused else None,
        "seamless_username": (seamless or {}).get("username")
        if paused
        else None,
        "username": username,
        "console": "spice",
        "driver": "vgpu3d",
        "viewer": viewer,
        "created_at": _now(),
    }
    _write_state(domain, state, state_dir)

    try:
        if report["seccomp_sandbox"] not in OFF_VALUES:
            state["host_changes"] = ensure_seccomp(
                current=report["seccomp_sandbox"],
                accept=accept_host_security_change,
                uri=uri,
                domain=domain,
            )
            _write_state(domain, state, state_dir)
        with LibvirtBackend(uri) as backend:
            backend.redefine_domain(domain, apply_xml(xml))
            backend.start(domain)
            _wait_qga(backend, domain)
            ssh = _prepare_ssh(
                backend, domain, username, state_dir=state_dir
            )
            ssh.run("powercfg /h off", timeout=60)
            result = ssh.run("bcdedit /set testsigning on", timeout=30)
            if not result.ok:
                raise VemuError(
                    "Cannot enable test signing: " + result.output
                )
            _reboot_guest(ssh, backend, domain)
            installed = _install_driver(ssh, installer)
            if installed not in (0, 3010):
                raise VemuError(
                    f"vgpu3d installer exited with code {installed}."
                )
            if installed == 3010:
                _reboot_guest(ssh, backend, domain)
            try:
                verified = _verify_driver(ssh)
            except DriverNotReady as exc:
                if exc.problem_code not in (None, 37):
                    raise
                state["driver_repair"] = _repair_boot_guard(ssh)
                _write_state(domain, state, state_dir)
                verified = _verify_driver(ssh)
        state["status"] = "enabled"
        state["driver_verify"] = verified
        state["enabled_at"] = _now()
        _write_state(domain, state, state_dir)
    except Exception as exc:
        if not keep_broken:
            try:
                _revert_state(
                    domain, state, uri=uri, state_dir=state_dir
                )
            except Exception as rollback:  # noqa: BLE001
                state["status"] = "rollback-failed"
                state["rollback_error"] = str(rollback)
                try:
                    _write_state(domain, state, state_dir)
                except OSError:
                    pass
                raise VemuError(
                    f"{exc}. Automatic rollback also failed: {rollback}. "
                    f"Run 'vemu experimental 3d revert {domain}' after "
                    "fixing the reported problem."
                ) from exc
        raise
    return {
        "domain": domain,
        "status": "enabled",
        "viewer": viewer,
        "installer": str(installer),
        "driver": verified,
        "seamless_paused": paused,
        "state": state,
    }
