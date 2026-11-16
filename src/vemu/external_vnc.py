"""External TigerVNC console support.

The desktop app and the CLI share this module so the viewer command and
process lifecycle stay identical.  It deliberately has no Qt dependency.
"""

from __future__ import annotations

import ipaddress
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ValidationError

VIEWER_BINARY = "vncviewer"
INSTALL_HINT = (
    "Install TigerVNC: tigervnc on Fedora/RHEL/openSUSE, or "
    "tigervnc-viewer on Debian/Ubuntu."
)


def find_vncviewer() -> str | None:
    """Return the TigerVNC viewer path, or None when it is not installed."""
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
            f"External VNC requires a literal loopback address, not '{raw}'."
        ) from exc
    if not address.is_loopback:
        raise ValidationError(
            f"External VNC only connects to loopback; '{raw}' is not loopback."
        )
    return "127.0.0.1" if address.version == 4 else "::1"


def viewer_endpoint(info: dict[str, Any]) -> tuple[str, int]:
    """Return a validated loopback host and VNC port."""
    host = _loopback_host(info.get("host"))
    try:
        port = int(info["port"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValidationError("The VNC port is missing or invalid.") from exc
    if not 1 <= port <= 65535:
        raise ValidationError(f"VNC port {port} is out of range.")
    return host, port


def viewer_address(host: str, port: int) -> str:
    if ":" in host:
        return f"[{host}]::{port}"
    return f"{host}::{port}"


def viewer_command(viewer: str, info: dict[str, Any]) -> list[str]:
    """Build the fixed TigerVNC command used by both UI and CLI."""
    host, port = viewer_endpoint(info)
    return [
        viewer,
        "-Shared=1",
        "-ViewOnly=0",
        "-SendClipboard=0",
        "-AcceptClipboard=0",
        "-SendPrimary=0",
        "-SetPrimary=0",
        "-RemoteResize=0",
        "-AlertOnFatalError=1",
        "-ReconnectOnError=1",
        viewer_address(host, port),
    ]


def viewer_state_dir() -> Path:
    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured) / "vemu"
    return Path.home() / ".local" / "state" / "vemu"


def viewer_log_path(domain: str) -> Path:
    safe = "".join(
        character if character.isalnum() or character in "-_." else "_"
        for character in domain
    ) or "vm"
    return viewer_state_dir() / f"viewer-{safe}.log"


@dataclass
class ViewerHandle:
    domain: str
    process: subprocess.Popen[Any]
    log_path: Path
    command: list[str]


class ViewerManager:
    """Track one detached TigerVNC process per VM."""

    def __init__(self) -> None:
        self._handles: dict[str, ViewerHandle] = {}

    def running(self, domain: str) -> bool:
        handle = self._handles.get(domain)
        if handle is None:
            return False
        if handle.process.poll() is None:
            return True
        self._handles.pop(domain, None)
        return False

    def handle(self, domain: str) -> ViewerHandle | None:
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
    ) -> ViewerHandle:
        if self.running(domain):
            raise ValidationError(
                f"TigerVNC is already open for '{domain}'."
            )
        executable = viewer or find_vncviewer()
        if not executable:
            raise ValidationError(
                "TigerVNC vncviewer was not found. " + INSTALL_HINT
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
                f"Could not write the TigerVNC log {log_path}: {exc}"
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
                    f"Could not start TigerVNC: {exc}"
                ) from exc
        handle = ViewerHandle(
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
                f"TigerVNC exited immediately (status {process.returncode}). "
                f"See {log_path}."
            )
        return handle
