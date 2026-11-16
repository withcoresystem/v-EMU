"""External SPICE console support (remote-viewer).

The embedded noVNC console speaks RFB/VNC only.  A domain with a SPICE
graphics device (the experimental virtio-gpu 3D setup) therefore needs an
external SPICE client.  This module mirrors :mod:`vemu.external_vnc`: a fixed
command, a loopback-only endpoint, one viewer process per VM and a log file.
It has no Qt dependency so the desktop app and the CLI share it.
"""

from __future__ import annotations

import ipaddress
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .external_vnc import viewer_state_dir

VIEWER_BINARY = "remote-viewer"
INSTALL_HINT = (
    "Install virt-viewer (the remote-viewer SPICE client): "
    "virt-viewer on Fedora/RHEL/openSUSE/Arch, or virt-viewer on "
    "Debian/Ubuntu."
)


def find_spiceviewer() -> str | None:
    """Return the remote-viewer path, or None when it is not installed."""
    return shutil.which(VIEWER_BINARY)


def viewer_install_hint() -> str:
    return INSTALL_HINT


def _loopback_host(host: object) -> str:
    raw = str(host or "127.0.0.1").strip()
    if raw.lower() in {"localhost", "ip6-localhost"}:
        return "127.0.0.1"
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise ValidationError(
            f"External SPICE requires a literal loopback address, not '{raw}'."
        ) from exc
    if not address.is_loopback:
        raise ValidationError(
            f"External SPICE only connects to loopback; '{raw}' is not loopback."
        )
    return "127.0.0.1" if address.version == 4 else "::1"


def viewer_endpoint(info: dict[str, Any]) -> tuple[str, int]:
    """Return a validated loopback host and SPICE port."""
    host = _loopback_host(info.get("host"))
    try:
        port = int(info["port"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("The SPICE port is missing or invalid.") from exc
    if not 1 <= port <= 65535:
        raise ValidationError(f"SPICE port {port} is out of range.")
    return host, port


def viewer_uri(host: str, port: int) -> str:
    if ":" in host:
        return f"spice://[{host}]:{port}"
    return f"spice://{host}:{port}"


def viewer_command(viewer: str, info: dict[str, Any]) -> list[str]:
    """Build the fixed remote-viewer command used by both UI and CLI.

    The flags mirror the safety choices in the TigerVNC command:

    * ``--shared`` keeps a second console from being refused.
    * ``--auto-resize=never`` avoids changing the guest resolution.
    * ``--spice-disable-usbredir`` leaves USB management to v-EMU.
    * ``--spice-disable-audio`` matches the VNC console, which has no audio.
    """
    host, port = viewer_endpoint(info)
    return [
        viewer,
        "--shared",
        "--auto-resize=never",
        "--spice-disable-usbredir",
        "--spice-disable-audio",
        viewer_uri(host, port),
    ]


def viewer_log_path(domain: str) -> Path:
    safe = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in domain
    ) or "vm"
    return viewer_state_dir() / f"spice-viewer-{safe}.log"


@dataclass
class SpiceViewerHandle:
    domain: str
    process: subprocess.Popen[Any]
    log_path: Path
    command: list[str]


class SpiceViewerManager:
    """Track one detached remote-viewer process per VM."""

    def __init__(self) -> None:
        self._handles: dict[str, SpiceViewerHandle] = {}

    def running(self, domain: str) -> bool:
        handle = self._handles.get(domain)
        if handle is None:
            return False
        if handle.process.poll() is None:
            return True
        self._handles.pop(domain, None)
        return False

    def handle(self, domain: str) -> SpiceViewerHandle | None:
        if not self.running(domain):
            return None
        return self._handles.get(domain)

    def launch(
        self,
        domain: str,
        info: dict[str, Any],
        *,
        viewer: str | None = None,
        settle_seconds: float = 0.15,
    ) -> SpiceViewerHandle:
        if self.running(domain):
            raise ValidationError(
                f"remote-viewer is already open for '{domain}'."
            )
        executable = viewer or find_spiceviewer()
        if not executable:
            raise ValidationError(
                "remote-viewer was not found. " + INSTALL_HINT
            )
        command = viewer_command(executable, info)
        log_path = viewer_log_path(domain)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.parent.chmod(0o700)
        except OSError:
            pass
        try:
            log = log_path.open("a", encoding="utf-8")
        except OSError as exc:
            raise ValidationError(
                f"Could not write the SPICE viewer log {log_path}: {exc}"
            ) from exc
        with log:
            log.write(
                f"--- {time.strftime('%Y-%m-%dT%H:%M:%S%z')} "
                + " ".join(command)
                + "\n"
            )
            log.flush()
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=log,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as exc:
                raise ValidationError(
                    f"Could not start remote-viewer: {exc}"
                ) from exc
        handle = SpiceViewerHandle(
            domain=domain,
            process=process,
            log_path=log_path,
            command=command,
        )
        self._handles[domain] = handle
        if settle_seconds > 0:
            time.sleep(settle_seconds)
        if process.poll() is not None:
            self._handles.pop(domain, None)
            raise ValidationError(
                f"remote-viewer exited immediately (status "
                f"{process.returncode}). See {log_path}."
            )
        return handle
