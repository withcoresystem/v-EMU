"""Host prerequisite checks shared by the CLI, daemon and desktop app."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from .backend import LibvirtBackend
from .chain import find_commit_journals
from .errors import ValidationError, VemuError
from .firmware import firmware_for, firmware_root, firmware_version
from .network import NetworkStore
from .qemuuser import qemu_identity
from .storage import StorageStore
from .tools import which as _which
from .virtcheck import virtualization_status


def _version(path: str) -> str:
    result = subprocess.run(
        [path, "--version"], capture_output=True, text=True, check=False
    )
    lines = result.stdout.splitlines()
    return lines[0] if lines else path


def _check(
    check: str,
    ok: bool,
    detail: str,
    *,
    required: bool = True,
) -> dict[str, Any]:
    return {
        "check": check,
        "ok": ok,
        "required": required,
        "level": "ok" if ok else ("fail" if required else "warn"),
        "detail": detail,
    }


def _selinux_mode() -> str:
    getenforce = _which("getenforce")
    if getenforce:
        result = subprocess.run(
            [getenforce], capture_output=True, text=True, check=False
        )
        value = result.stdout.strip()
        if value:
            return value
    enforce = Path("/sys/fs/selinux/enforce")
    if enforce.is_file():
        try:
            return (
                "Enforcing"
                if enforce.read_text(encoding="utf-8").strip() == "1"
                else "Permissive"
            )
        except OSError:
            pass
    return "not installed"


def _active_lsms() -> list[str]:
    """Return the LSM names enabled in the running kernel."""
    try:
        text = Path("/sys/kernel/security/lsm").read_text(encoding="utf-8")
    except OSError:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def _security_status() -> dict[str, Any]:
    """Describe SELinux on the host.

    Fedora and RHEL-family hosts use SELinux. AppArmor was only needed for the
    removed Debian/Ubuntu host path, so it is no longer probed.
    """
    lsms = _active_lsms()
    try:
        selinux_mode = _selinux_mode()
    except OSError:
        selinux_mode = "not installed"
    selinux_active = (
        selinux_mode in {"Enforcing", "Permissive"}
        and ("selinux" in lsms or not lsms)
    )
    return {
        "active": "selinux" if selinux_active else "none",
        "lsm": lsms,
        "selinux": {
            "mode": selinux_mode,
            "active": selinux_active,
        },
    }


def _read_ip_forward() -> str:
    """Read net.ipv4.ip_forward without depending on a subprocess."""
    try:
        value = Path("/proc/sys/net/ipv4/ip_forward").read_text(
            encoding="utf-8"
        ).strip()
        if value:
            return value
    except OSError:
        pass
    for command in (
        ["sysctl", "-n", "net.ipv4.ip_forward"],
        ["cat", "/proc/sys/net/ipv4/ip_forward"],
    ):
        try:
            value = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        except OSError:
            continue
        if value:
            return value
    return "unknown"


def _passthrough_check(domains) -> dict[str, Any]:
    """Report domains downgraded to External because of host passthrough."""
    passthrough = [
        item for item in domains if getattr(item, "passthrough", False)
    ]
    if not passthrough:
        return _check(
            "host_passthrough",
            True,
            "no host passthrough domains",
            required=False,
        )
    summary = ", ".join(
        f"{item.name} ({item.passthrough_detail})" for item in passthrough
    )
    check = _check(
        "host_passthrough",
        False,
        f"read-only External: {summary}; remove the hostdev to manage these "
        "VMs in v-EMU",
        required=False,
    )
    check["domains"] = [item.name for item in passthrough]
    return check


def _ip_forward_check(
    value: str, *, nat_registered: bool
) -> dict[str, Any]:
    """Warn when forwarding is unknown/off without a NAT network.

    Forwarding only matters once a NAT profile exists; a fresh host with no
    vSwitch must not show a hard FAIL just because the sysctl cannot be read.
    """
    value = (value or "unknown").strip()
    if value == "1":
        return _check("ip_forward", True, "1", required=False)
    scope = (
        "NAT network registered"
        if nat_registered
        else "no NAT network registered"
    )
    return _check(
        "ip_forward",
        False,
        f"{value} ({scope})",
        required=bool(nat_registered and value == "0"),
    )


def _tool_check(
    tool: str,
    *,
    required: bool = True,
    hint: str = "",
) -> dict[str, Any]:
    path = _which(tool)
    detail = path or (f"not found{': ' + hint if hint else ''}")
    return _check(tool, path is not None, detail, required=required)


def _qemu_system_x86() -> str | None:
    """Return the x86 system emulator path used by Fedora/RHEL/openSUSE."""
    path = shutil.which("qemu-system-x86_64")
    if path:
        return path
    fallback = Path("/usr/libexec/qemu-kvm")
    return str(fallback) if fallback.is_file() else None


def _experimental3d_status() -> dict[str, Any]:
    """Report the experimental 3D host security state for Doctor."""
    try:
        from . import experimental3d

        states = experimental3d.list_states()
        seccomp = experimental3d.seccomp_value()
    except Exception as exc:  # noqa: BLE001 - Doctor must stay usable
        return {
            "check": _check(
                "Experimental 3D host security",
                False,
                f"cannot inspect state: {exc}",
                required=False,
            ),
            "data": {"states": [], "restorable": False},
        }
    backup = Path("/etc/libvirt/qemu.conf.vemu-3d-backup")
    enabled = [
        item for item in states if item.get("enabled")
    ]
    off = seccomp in {"0", "false", "off", "no"}
    restorable = bool(off and not enabled)
    if seccomp == "unknown":
        check = _check(
            "Experimental 3D host security",
            False,
            "cannot read seccomp_sandbox from /etc/libvirt/qemu.conf",
            required=False,
        )
    elif off:
        detail = (
            f"seccomp_sandbox={seccomp}; "
            f"{len(states)} experimental 3D state(s)"
        )
        if enabled:
            detail += " (enabled: " + ", ".join(
                str(item.get("domain")) for item in enabled
            ) + ")"
        if backup.is_file():
            detail += f"; backup {backup}"
        else:
            detail += "; no backup; restore will set seccomp_sandbox=1"
        check = _check(
            "Experimental 3D host security",
            False,
            detail,
            required=False,
        )
    else:
        detail = f"seccomp_sandbox={seccomp}; no experimental 3D host change"
        check = _check(
            "Experimental 3D host security", True, detail, required=False
        )
    return {
        "check": check,
        "data": {
            "states": states,
            "enabled": [str(item.get("domain")) for item in enabled],
            "seccomp_sandbox": seccomp,
            "backup": str(backup) if backup.is_file() else "",
            "fallback": not backup.is_file(),
            "restorable": restorable,
        },
    }


def run_checks(uri: str = "qemu:///system", *, config: str | None = None) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    qemu_img = _which("qemu-img")
    checks.append(
        _check(
            "qemu-img",
            qemu_img is not None,
            _version(qemu_img) if qemu_img else "not found",
        )
    )
    qemu_system = _qemu_system_x86()
    checks.append(
        _check(
            "qemu-system-x86_64",
            qemu_system is not None,
            qemu_system or "not found",
        )
    )
    try:
        qemu = qemu_identity()
    except ValidationError as exc:
        checks.append(
            _check("QEMU user", False, str(exc))
        )
    else:
        checks.append(
            _check(
                "QEMU user",
                True,
                f"{qemu.owner} ({qemu.source})",
            )
        )
    checks.append(_tool_check("virsh", hint="install libvirt-client"))
    for tool in ("virt-customize", "virt-cat", "virt-ls"):
        checks.append(
            _tool_check(tool, hint="install guestfs-tools")
        )
    available_firmware = [
        arch for arch in ("x86_64", "aarch64", "riscv64")
        if firmware_for(arch) is not None
    ]
    checks.append(
        _check(
            "v-EMU firmware",
            bool(available_firmware),
            (
                ", ".join(available_firmware)
                + (
                    f" (firmware {firmware_version()})"
                    if firmware_version()
                    else ""
                )
            )
            if available_firmware
            else f"not installed under {firmware_root()} "
            "(run ./setup.sh --daemon; stock firmware is used as fallback)",
            required=False,
        )
    )
    checks.append(_tool_check("xorriso", hint="needed for unattended ISOs"))
    checks.append(_tool_check("mkfs.vfat", hint="needed for answer floppies"))
    checks.append(_tool_check("mcopy", hint="needed for answer floppies"))
    checks.append(
        _tool_check(
            "swtpm",
            required=False,
            hint="optional TPM 2.0 emulator",
        )
    )
    checks.append(
        _tool_check(
            "qemu-system-aarch64",
            required=False,
            hint="optional aarch64 TCG guests",
        )
    )
    checks.append(
        _tool_check(
            "qemu-system-riscv64",
            required=False,
            hint="optional riscv64 TCG guests",
        )
    )
    for name, arch, path in (
        ("edk2-aarch64", "aarch64", "/usr/share/edk2/aarch64/QEMU_EFI-pflash.raw"),
        ("edk2-riscv64", "riscv64", "/usr/share/edk2/riscv/RISCV_VIRT_CODE.fd"),
    ):
        custom = firmware_for(arch)
        if custom is not None:
            detail = f"v-EMU custom firmware: {custom.loader}"
            exists = True
        else:
            exists = Path(path).is_file()
            detail = path if exists else f"not found: {path}"
        checks.append(
            _check(name, exists, detail, required=False)
        )
    virtiofsd = _which("virtiofsd")
    if virtiofsd is None:
        for candidate in (
            "/usr/libexec/virtiofsd",
            "/usr/lib/virtiofsd",
        ):
            if Path(candidate).is_file():
                virtiofsd = candidate
                break
    checks.append(
        _check(
            "virtiofsd",
            bool(virtiofsd),
            str(virtiofsd)
            if virtiofsd
            else "not found (install virtiofsd for VirtioFS shares)",
            required=False,
        )
    )
    checks.append(_tool_check("nft", hint="install nftables"))

    virtualization = virtualization_status()
    cpu_ok = bool(virtualization["cpu_virtualization"]) or not virtualization[
        "x86"
    ]
    cpu_detail = str(virtualization["cpu_detail"])
    if not cpu_ok and virtualization["hint"]:
        cpu_detail += f". {virtualization['hint']}"
    checks.append(_check("CPU virtualization", cpu_ok, cpu_detail))
    kvm_detail = str(virtualization["kvm_detail"])
    if not virtualization["kvm_device"] and virtualization["hint"]:
        kvm_detail += f". {virtualization['hint']}"
    checks.append(
        _check(
            "/dev/kvm",
            bool(virtualization["kvm_device"]),
            kvm_detail,
        )
    )
    if virtualization["virtualization"] not in {"none", "unknown", "bare"}:
        checks.append(
            _check(
                "virtualization environment",
                True,
                f"{virtualization['virtualization']} guest; "
                + (
                    "nested KVM is available"
                    if virtualization["kvm_device"]
                    else "nested KVM is not exposed"
                ),
                required=False,
            )
        )

    nat_registered = False
    try:
        nat_registered = any(
            str(getattr(profile, "mode", "")) == "nat"
            for profile in NetworkStore().load()
        )
    except (OSError, ValueError, VemuError):
        pass
    checks.append(
        _ip_forward_check(
            _read_ip_forward(), nat_registered=nat_registered
        )
    )

    try:
        with LibvirtBackend(uri) as backend:
            domains = backend.list_domains()
        checks.append(
            _check(
                "libvirt",
                True,
                f"{len(domains)} domain(s) at {uri}",
            )
        )
        drifted = [item.name for item in domains if item.drift == "drifted"]
        drift_check = _check(
            "domain_drift",
            not drifted,
            "no XML drift"
            if not drifted
            else "XML drifted from baseline: " + ", ".join(drifted),
            required=False,
        )
        if drifted:
            drift_check["domains"] = drifted
        checks.append(drift_check)
        checks.append(_passthrough_check(domains))
        try:
            with LibvirtBackend(uri) as backend:
                backend.usb_devices()
        except VemuError as exc:
            checks.append(
                _check(
                    "USB devices",
                    False,
                    str(exc),
                    required=False,
                )
            )
        else:
            checks.append(
                _check("USB devices", True, "nodedev available", required=False)
            )
    except VemuError as exc:
        checks.append(_check("libvirt", False, str(exc)))

    try:
        locations = StorageStore(config).load()
        journals: list[Path] = []
        for location in locations:
            journals.extend(find_commit_journals(location["path"]))
        if journals:
            preview = ", ".join(str(path) for path in journals[:3])
            if len(journals) > 3:
                preview += f", +{len(journals) - 3} more"
            checks.append(
                _check(
                    "chain_commit_journal",
                    False,
                    f"unfinished commit(s): {preview}; run "
                    "'vemu doctor --repair'",
                )
            )
        else:
            checks.append(
                _check(
                    "chain_commit_journal",
                    True,
                    "no unfinished snapshot commits",
                )
            )
    except (OSError, ValueError) as exc:
        checks.append(
            _check(
                "chain_commit_journal",
                False,
                f"cannot scan storage locations: {exc}",
                required=False,
            )
        )

    security = _security_status()
    selinux_mode = security["selinux"]["mode"]
    checks.append(
        _check(
            "SELinux",
            security["selinux"]["active"] or selinux_mode == "Disabled",
            selinux_mode,
            required=False,
        )
    )

    experimental3d = _experimental3d_status()
    checks.append(experimental3d["check"])

    return {
        "ok": all(item["ok"] for item in checks if item["required"]),
        "checks": checks,
        "security": security,
        "experimental3d": experimental3d["data"],
    }
