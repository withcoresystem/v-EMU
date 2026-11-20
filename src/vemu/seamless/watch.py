"""Release RAIL input grabs when the last remote window closes.

FreeRDP's X11 RAIL client can leave a pointer/button grab behind after the
final RemoteApp window is destroyed, which makes host clicks disappear even
though the pointer still moves.  A small user-session watchdog stops the
supervisor shortly after the RAIL window count reaches zero; the Windows
session and its apps stay alive, and the next shortcut reconnects.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..errors import VemuError
from .launch import supervisor_paths, supervisor_pid

_WINDOW = re.compile(r"0x[0-9a-fA-F]+")


@dataclass(frozen=True)
class WatchConfig:
    domain: str
    state_dir: Path
    grace: float = 3.0
    startup_timeout: float = 60.0
    interval: float = 1.0


def _xprop() -> str | None:
    return shutil.which("xprop")


def rail_windows(display: str | None = None) -> int:
    xprop = _xprop()
    if not xprop:
        return 0
    environment = os.environ.copy()
    if display:
        environment["DISPLAY"] = display
    try:
        root = subprocess.run(
            [xprop, "-root", "_NET_CLIENT_LIST"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    if root.returncode != 0:
        return 0
    count = 0
    for window_id in _WINDOW.findall(root.stdout):
        try:
            info = subprocess.run(
                [xprop, "-id", window_id, "WM_CLASS"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
                env=environment,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if info.returncode == 0 and '"RAIL"' in info.stdout:
            count += 1
    return count


def run_watch(config: WatchConfig) -> int:
    from .. import service

    pid_path, _log = supervisor_paths(config.state_dir, config.domain)
    deadline = time.monotonic() + config.startup_timeout
    seen = False
    while time.monotonic() < deadline:
        if supervisor_pid(pid_path) is None:
            return 0
        if rail_windows():
            seen = True
            break
        time.sleep(config.interval)
    if not seen:
        return 0
    empty_since: float | None = None
    while supervisor_pid(pid_path) is not None:
        if rail_windows():
            empty_since = None
        elif empty_since is None:
            empty_since = time.monotonic()
        elif time.monotonic() - empty_since >= config.grace:
            try:
                service.vm_seamless_stop(
                    config.domain, state_dir=config.state_dir
                )
            except VemuError:
                pass
            return 0
        time.sleep(config.interval)
    return 0


def spawn(
    domain: str,
    state_dir: str | Path,
    *,
    display: str | None = None,
) -> int | None:
    """Spawn a detached watchdog; safe to call without a desktop."""
    if not _xprop() or not (display or os.environ.get("DISPLAY")):
        return None
    base = Path(state_dir).expanduser()
    base.mkdir(parents=True, exist_ok=True)
    log_path = base / f"{domain}.rail.watch.log"
    environment = os.environ.copy()
    if display:
        environment["DISPLAY"] = display
    log = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "vemu.seamless.watch",
                domain,
                "--state-dir",
                str(base),
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
        )
    finally:
        log.close()
    return process.pid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain")
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--grace", type=float, default=3.0)
    parser.add_argument("--startup-timeout", type=float, default=60.0)
    args = parser.parse_args(argv)
    config = WatchConfig(
        domain=args.domain,
        state_dir=Path(args.state_dir),
        grace=args.grace,
        startup_timeout=args.startup_timeout,
    )
    return run_watch(config)


if __name__ == "__main__":
    raise SystemExit(main())
