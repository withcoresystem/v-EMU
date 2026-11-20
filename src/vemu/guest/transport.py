"""Transport-neutral guest command interface."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence


@dataclass(frozen=True)
class CommandResult:
    """Result of one guest command."""

    exitcode: int | None
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.exitcode == 0

    @property
    def output(self) -> str:
        return self.stdout or self.stderr


class GuestTransport(Protocol):
    """A command/file channel into one guest."""

    def ping(self) -> bool: ...

    def run(
        self,
        path: str,
        args: Sequence[str] = (),
        *,
        timeout: int = 30,
    ) -> CommandResult: ...

    def run_powershell(
        self,
        script: str,
        *,
        timeout: int = 120,
    ) -> CommandResult: ...

    def upload(self, data: bytes, remote: str) -> None: ...

    def download(self, remote: str, *, max_bytes: int = 4 * 1024 * 1024) -> bytes: ...
