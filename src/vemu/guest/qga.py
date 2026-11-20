"""QEMU guest-agent transport for Windows guests."""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Sequence

from ..errors import VemuError
from .transport import CommandResult

POWERSHELL = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
CHUNK = 64 * 1024


class QGATransport:
    """Run commands and move files through the QEMU guest agent."""

    def __init__(self, backend: Any, domain: str) -> None:
        self.backend = backend
        self.domain = domain

    def _call(self, command: dict[str, Any], *, timeout: int = 30) -> Any:
        return self.backend.qemu_agent_command(
            self.domain, command, timeout=timeout
        )

    def ping(self) -> bool:
        try:
            self._call({"execute": "guest-ping"}, timeout=10)
        except VemuError:
            return False
        return True

    def run(
        self,
        path: str,
        args: Sequence[str] = (),
        *,
        timeout: int = 30,
    ) -> CommandResult:
        payload = self._call(
            {
                "execute": "guest-exec",
                "arguments": {
                    "path": path,
                    "arg": list(args),
                    "capture-output": True,
                },
            },
            timeout=15,
        )
        if not isinstance(payload, dict) or "pid" not in payload:
            raise VemuError(f"Guest did not start {path}.")
        pid = int(payload["pid"])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self._call(
                {
                    "execute": "guest-exec-status",
                    "arguments": {"pid": pid},
                },
                timeout=15,
            )
            if not isinstance(status, dict):
                raise VemuError("Guest returned an invalid exec status.")
            if status.get("exited"):
                return CommandResult(
                    exitcode=(
                        int(status["exitcode"])
                        if status.get("exitcode") is not None
                        else None
                    ),
                    stdout=base64.b64decode(
                        status.get("out-data", "")
                    ).decode("utf-8", "replace"),
                    stderr=base64.b64decode(
                        status.get("err-data", "")
                    ).decode("utf-8", "replace"),
                )
            time.sleep(0.2)
        raise VemuError(f"Guest command timed out: {path}")

    def run_powershell(
        self,
        script: str,
        *,
        timeout: int = 120,
    ) -> CommandResult:
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        return self.run(
            POWERSHELL,
            [
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            timeout=timeout,
        )

    def upload(self, data: bytes, remote: str) -> None:
        handle = self._call(
            {
                "execute": "guest-file-open",
                "arguments": {"path": remote, "mode": "wb"},
            },
            timeout=15,
        )
        if handle is None:
            raise VemuError(f"Cannot open guest file for writing: {remote}")
        try:
            for offset in range(0, len(data), CHUNK):
                chunk = data[offset : offset + CHUNK]
                self._call(
                    {
                        "execute": "guest-file-write",
                        "arguments": {
                            "handle": handle,
                            "buf-b64": base64.b64encode(chunk).decode("ascii"),
                            "count": len(chunk),
                        },
                    },
                    timeout=15,
                )
        finally:
            self._call(
                {
                    "execute": "guest-file-close",
                    "arguments": {"handle": handle},
                },
                timeout=10,
            )

    def download(
        self,
        remote: str,
        *,
        max_bytes: int = 4 * 1024 * 1024,
    ) -> bytes:
        handle = self._call(
            {
                "execute": "guest-file-open",
                "arguments": {"path": remote, "mode": "rb"},
            },
            timeout=15,
        )
        if handle is None:
            raise VemuError(f"Cannot open guest file for reading: {remote}")
        output = bytearray()
        try:
            while True:
                payload = self._call(
                    {
                        "execute": "guest-file-read",
                        "arguments": {
                            "handle": handle,
                            "count": CHUNK,
                        },
                    },
                    timeout=15,
                )
                if not isinstance(payload, dict):
                    break
                chunk = payload.get("buf-b64") or ""
                if chunk:
                    output.extend(base64.b64decode(str(chunk)))
                if len(output) > max_bytes:
                    raise VemuError(
                        f"Guest file is larger than {max_bytes} bytes: {remote}"
                    )
                if not chunk or payload.get("eof"):
                    break
        finally:
            self._call(
                {
                    "execute": "guest-file-close",
                    "arguments": {"handle": handle},
                },
                timeout=10,
            )
        return bytes(output)

    def read_text(self, remote: str, *, max_bytes: int = 1024 * 1024) -> str:
        return self.download(remote, max_bytes=max_bytes).decode(
            "utf-8", "replace"
        )

    def write_text(self, text: str, remote: str) -> None:
        self.upload(text.encode("utf-8"), remote)

    # Kept as a tiny convenience for diagnostics that want JSON.
    @staticmethod
    def parse_json(result: CommandResult) -> Any:
        return json.loads(result.output)
