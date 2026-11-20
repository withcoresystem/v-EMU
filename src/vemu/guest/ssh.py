"""Key-only SSH transport for Windows guests."""

from __future__ import annotations

import base64
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..errors import VemuError
from .transport import CommandResult

POWERSHELL = (
    r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
)

_NETWORK_HINT = (
    "vemud cannot create IPv4/IPv6 sockets. Add "
    "AF_INET AF_INET6 to RestrictAddressFamilies in the vemud unit, "
    "then run 'systemctl daemon-reload && systemctl restart vemud'."
)


def explain_ssh_error(text: str) -> str:
    """Add a fix hint when the daemon sandbox blocks AF_INET."""
    if "Address family not supported by protocol" in text:
        return f"{text.rstrip()}\n{_NETWORK_HINT}"
    return text


def ensure_keypair(path: Path) -> Path:
    """Create an ed25519 keypair when missing and return the public key path."""
    path = path.expanduser()
    pub = Path(str(path) + ".pub")
    if not path.is_file() or not pub.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [
                "ssh-keygen",
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise VemuError(
                f"Cannot create SSH key: {result.stderr.strip()}"
            )
    try:
        os.chmod(path, 0o600)
        os.chmod(pub, 0o644)
    except OSError:
        # Shared-state keys may be owned by the daemon; group ACLs apply.
        pass
    return pub


def scan_host_key(
    host: str,
    known_hosts: Path,
    *,
    port: int = 22,
    timeout: int = 10,
) -> str:
    """Pin the guest SSH host key in a dedicated known_hosts file."""
    result = subprocess.run(
        [
            "ssh-keyscan",
            "-T",
            str(timeout),
            "-p",
            str(port),
            host,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise VemuError(
            "Cannot read the guest SSH host key: "
            + (result.stderr.strip() or "no response")
        )
    known_hosts = known_hosts.expanduser()
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    known_hosts.write_text(result.stdout, encoding="utf-8")
    try:
        os.chmod(known_hosts, 0o600)
    except OSError:
        pass
    return result.stdout


@dataclass
class SSHTransport:
    """Run guest commands over a pinned, key-only OpenSSH connection."""

    host: str
    user: str
    key: Path
    known_hosts: Path
    port: int = 22
    connect_timeout: int = 10

    def _base(self) -> list[str]:
        ssh = "ssh"
        return [
            ssh,
            "-F",
            "/dev/null",
            "-i",
            str(self.key),
            "-p",
            str(self.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
            "-o",
            f"ConnectTimeout={self.connect_timeout}",
            f"{self.user}@{self.host}",
        ]

    def run(
        self,
        remote_command: str,
        *,
        timeout: int = 60,
    ) -> CommandResult:
        command = [*self._base(), remote_command]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise VemuError(
                f"SSH command timed out for {self.user}@{self.host}."
            ) from exc
        return CommandResult(
            exitcode=result.returncode,
            stdout=result.stdout,
            stderr=explain_ssh_error(result.stderr),
        )

    def run_powershell(
        self,
        script: str,
        *,
        timeout: int = 120,
    ) -> CommandResult:
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        command = (
            f'"{POWERSHELL}" -NoProfile -NonInteractive '
            f"-ExecutionPolicy Bypass -EncodedCommand {encoded}"
        )
        return self.run(command, timeout=timeout)

    def ping(self) -> bool:
        return self.run("echo ok", timeout=10).stdout.strip() == "ok"

    def upload(self, data: bytes, remote: str) -> None:
        scp = "scp"
        command = [
            scp,
            "-F",
            "/dev/null",
            "-i",
            str(self.key),
            "-P",
            str(self.port),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self.known_hosts}",
        ]
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as handle:
            handle.write(data)
            source = handle.name
        try:
            result = subprocess.run(
                [*command, source, f"{self.user}@{self.host}:{remote}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        finally:
            os.unlink(source)
        if result.returncode != 0:
            raise VemuError(
                "SCP upload failed: "
                + explain_ssh_error(
                    result.stderr.strip() or result.stdout.strip()
                )
            )

    def download(
        self,
        remote: str,
        *,
        max_bytes: int = 4 * 1024 * 1024,
    ) -> bytes:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "download"
            result = subprocess.run(
                [
                    "scp",
                    "-F",
                    "/dev/null",
                    "-i",
                    str(self.key),
                    "-P",
                    str(self.port),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={self.known_hosts}",
                    f"{self.user}@{self.host}:{remote}",
                    str(dest),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if result.returncode != 0:
                raise VemuError(
                    "SCP download failed: "
                    + explain_ssh_error(
                        result.stderr.strip() or result.stdout.strip()
                    )
                )
            if dest.stat().st_size > max_bytes:
                raise VemuError(
                    f"Guest file is larger than {max_bytes} bytes: {remote}"
                )
            return dest.read_bytes()
