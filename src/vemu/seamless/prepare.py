"""Seamless-mode prepare pipeline.

The pipeline is transport-neutral: callers pass a QGA or SSH transport.  It
applies the shared Windows optimization, installs the pinned SSH key, enables
RDP and registers the RemoteApp allowlist.  Passwords are never accepted or
stored here; only the username is needed for setup.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..errors import VemuError
from ..guest.optimize import OptimizationReport
from ..guest.optimize import apply_profile as apply_optimization
from ..guest.ssh import SSHTransport, ensure_keypair, scan_host_key
from ..guest.transport import GuestTransport
from ..guest.windows import ensure_service, pending_reboot
from .apps import with_compat_arguments
from .overlay import apply_overlay
from .rdp import (
    add_remoteapp_app,
    configure_ssh_key_only,
    enable_rdp,
    inspect_windows_user,
    install_admin_key,
    install_user_key,
    preflight,
    rdp_certificate_sha256,
    remoteapp_state,
)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
SHARED_STATE = Path("/var/lib/vemu/seamless")


def _state_group() -> int | None:
    try:
        import grp

        return grp.getgrnam("vemu").gr_gid
    except (ImportError, KeyError):
        return None


def _is_shared(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(SHARED_STATE)
    except (OSError, ValueError):
        return False


def _share(path: Path, mode: int) -> None:
    """Give the vemu group access to a shared-state file."""
    if not _is_shared(path):
        return
    group = _state_group()
    try:
        if group is not None:
            os.chown(path, -1, group)
        os.chmod(path, mode)
    except OSError:
        pass


def _ensure_shared_dir() -> None:
    if os.geteuid() != 0:
        return
    try:
        SHARED_STATE.mkdir(mode=0o770, parents=True, exist_ok=True)
        group = _state_group()
        if group is not None:
            os.chown(SHARED_STATE, -1, group)
        os.chmod(SHARED_STATE, 0o770)
    except OSError:
        pass


def _has_state(path: Path) -> bool:
    try:
        return any(path.glob("*.json"))
    except OSError:
        return False


def state_dir() -> Path:
    override = os.environ.get("VEMU_SEAMLESS_STATE_DIR")
    if override:
        return Path(override).expanduser()
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured) / "vemu" / "seamless"
    user_state = Path.home() / ".local" / "state" / "vemu" / "seamless"
    # Existing desktop activations may predate the shared /var/lib directory.
    # Prefer the user state only when it actually contains seamless state.
    if _has_state(user_state):
        return user_state
    if SHARED_STATE.exists() and os.access(
        SHARED_STATE, os.R_OK | os.W_OK | os.X_OK
    ):
        return SHARED_STATE
    if os.geteuid() == 0:
        _ensure_shared_dir()
        if SHARED_STATE.exists():
            return SHARED_STATE
    return user_state


def _adopt_owner(path: Path, directory: Path) -> None:
    """When root writes into a user's state dir, hand the files back."""
    if os.geteuid() != 0:
        return
    try:
        info = directory.stat()
        os.chown(path, info.st_uid, info.st_gid)
    except OSError:
        pass


def _safe_name(value: str, label: str) -> str:
    if not _SAFE_NAME.match(value):
        raise VemuError(f"{label} may only contain letters, numbers, . _ -")
    return value


@dataclass(frozen=True)
class SeamlessPrepare:
    host: str
    username: str
    domain: str = "windows"
    profile: str = "Safe"
    apps: Mapping[str, Any] | None = None
    optimize: bool = True
    overlay: bool = True
    video: str = ""
    user_hive: str | None = None
    key_path: Path | None = None
    known_hosts: Path | None = None
    state_path: Path | None = None
    owner_uid: int | None = None
    ssh_attempts: int = 90
    ssh_delay: float = 1.0


def write_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(
            path.parent, 0o770 if _is_shared(path.parent) else 0o700
        )
    except OSError:
        pass
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    _adopt_owner(temporary, path.parent)
    temporary.replace(path)
    _share(path, 0o660)


def state_path_for(domain: str) -> Path:
    return state_dir() / f"{_safe_name(domain, 'VM name')}.json"


def read_state(domain: str) -> dict[str, Any] | None:
    path = state_path_for(domain)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VemuError(
            f"Cannot read seamless state for '{domain}': {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise VemuError(f"Invalid seamless state for '{domain}'.")
    return data


def _wait_ssh_ready(
    ssh: SSHTransport, *, attempts: int, delay: float
) -> tuple[bool, str]:
    """Wait for key login, treating a per-attempt timeout as retryable.

    ``SSHTransport.run`` raises ``VemuError`` when the local ``ssh`` process
    hits its own timeout.  A guest that is still starting sshd can accept TCP
    but not finish the handshake yet, so a timeout must not abort the whole
    prepare; retry like a non-zero exit.
    """
    last_error = ""
    total = max(1, attempts)
    for attempt in range(total):
        try:
            result = ssh.run("echo ok", timeout=10)
        except VemuError as exc:
            last_error = str(exc)
        else:
            if result.stdout.strip() == "ok":
                return True, ""
            last_error = result.stderr.strip() or result.stdout.strip()
        if attempt + 1 < total:
            time.sleep(delay)
    return False, last_error


def prepare_seamless(
    transport: GuestTransport,
    plan: SeamlessPrepare,
) -> dict[str, Any]:
    """Run the full guest-side setup and return the recorded state.

    Raises :class:`VemuError` when preflight, optimization, SSH or RDP setup
    fails.  The state file never contains a password or private key material.
    """
    username = _safe_name(plan.username, "Windows username")
    domain = _safe_name(plan.domain, "VM name")
    directory = state_dir()
    key_path = plan.key_path or directory / f"{domain}.id_ed25519"
    known_hosts = plan.known_hosts or directory / f"{domain}.known_hosts"
    state_path = plan.state_path or directory / f"{domain}.json"

    report = preflight(transport)
    if not report["supported"]:
        raise VemuError(
            "The guest is not ready for seamless mode: "
            + "; ".join(report["reasons"])
        )
    if not report["ssh_listening"]:
        raise VemuError("The guest is not listening on SSH port 22.")

    user_info = inspect_windows_user(transport, username)
    if not user_info.get("admin") and not user_info.get("profile"):
        raise VemuError(
            f"Windows profile for '{username}' does not exist yet. Log into "
            "Windows as that user once (console or RDP), then rerun prepare. "
            "The user has already been added to Remote Desktop Users."
        )

    optimization: OptimizationReport | None = None
    if plan.optimize:
        optimization = apply_optimization(
            transport,
            plan.profile,
            user_hive=plan.user_hive,
        )
    overlay = apply_overlay(transport) if plan.overlay else None

    service_report: dict[str, Any] = {}
    service_warnings: list[str] = []
    for service_name in ("sshd", "ssh-agent", "WinFsp.Launcher"):
        service_result = ensure_service(
            transport, service_name, startup="Automatic", start=True
        )
        service_report[service_name] = service_result
        if service_result.get("error"):
            service_warnings.append(
                f"{service_name}: {service_result['error']}"
            )
    launcher_present = bool(
        service_report.get("WinFsp.Launcher", {}).get("present")
    )
    if not launcher_present:
        service_result = ensure_service(
            transport, "VirtioFsSvc", startup="Automatic", start=True
        )
        service_report["VirtioFsSvc"] = service_result
        if service_result.get("error"):
            service_warnings.append(
                f"VirtioFsSvc: {service_result['error']}"
            )

    public_key = ensure_keypair(key_path)
    _adopt_owner(key_path, key_path.parent)
    _adopt_owner(public_key, key_path.parent)
    _share(key_path, 0o600)
    _share(public_key, 0o644)
    if plan.owner_uid is not None:
        try:
            os.chown(key_path, plan.owner_uid, -1)
        except OSError:
            pass
    if user_info.get("admin"):
        install_admin_key(
            transport, public_key.read_text(encoding="utf-8")
        )
    else:
        install_user_key(
            transport,
            username,
            public_key.read_text(encoding="utf-8"),
        )
    scan_host_key(plan.host, known_hosts)
    _adopt_owner(known_hosts, known_hosts.parent)
    _share(known_hosts, 0o660)

    rdp = enable_rdp(transport)
    if rdp.get("fDenyTSConnections") != 0:
        raise VemuError("RDP is still disabled after setup.")
    rdp_cert = rdp_certificate_sha256(transport)
    apps: list[dict[str, Any]] = []
    for name, value in (plan.apps or {}).items():
        if isinstance(value, Mapping):
            path = str(value.get("path") or "")
            arguments = str(value.get("arguments") or "")
            verified = bool(value.get("verified"))
            icon = value.get("icon")
            shortcut = value.get("shortcut")
        else:
            path = str(value)
            arguments = ""
            verified = False
            icon = None
            shortcut = None
        if not path:
            continue
        combined = with_compat_arguments(path, arguments)
        add_remoteapp_app(
            transport,
            name,
            path,
            required_command_line=combined,
        )
        entry: dict[str, Any] = {
            "name": name,
            "path": path,
            "arguments": combined,
            "verified": verified,
        }
        if icon:
            entry["icon"] = str(icon)
        if shortcut:
            entry["shortcut"] = str(shortcut)
        apps.append(entry)

    ssh = SSHTransport(plan.host, username, key_path, known_hosts)
    ready, last_error = _wait_ssh_ready(
        ssh, attempts=plan.ssh_attempts, delay=plan.ssh_delay
    )
    if not ready:
        detail = f" Last error: {last_error}" if last_error else ""
        raise VemuError(
            f"SSH key login as {username}@{plan.host} did not become ready "
            f"after {max(1, plan.ssh_attempts)} attempts."
            + detail
        )
    reboot_pending = pending_reboot(transport)
    ssh_hardening: dict[str, Any] = {}
    ssh_warning = ""
    try:
        # sshd_config and the service are machine-wide; use QGA/SYSTEM so a
        # standard user does not need local administrator rights.
        ssh_hardening = configure_ssh_key_only(transport)
    except VemuError as exc:
        ssh_warning = str(exc)

    state = {
        "branch": "seamless",
        "domain": domain,
        "host": plan.host,
        "username": username,
        "windows_user": user_info,
        "active": True,
        "profile": plan.profile if plan.optimize else None,
        "apps": apps,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "guest": report["info"],
        "video": plan.video,
        "overlay": overlay,
        "rdp_cert_sha256": rdp_cert,
        "ssh": ssh_hardening,
        "reboot_required": reboot_pending,
        "reboot_recommended": True,
        "services": service_report,
        "virtiofs_mode": (
            "winfsp-launcher" if launcher_present else "single-service"
        ),
        "preflight": {
            "services": report["services"],
            "winfsp": report["winfsp"],
        },
        "optimization": optimization.state if optimization else None,
        "rdp": remoteapp_state(transport),
        "key_path": str(key_path),
        "known_hosts": str(known_hosts),
    }
    if ssh_warning:
        state["ssh_warning"] = ssh_warning
    if service_warnings:
        state["service_warnings"] = service_warnings
    write_state(state_path, state)
    return state
