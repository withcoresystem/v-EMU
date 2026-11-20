#!/usr/bin/env python3
"""Upload and launch the Windows seamless soak test.

The workload runs in the logged-on RemoteApp session, not in session 0. It
needs a prepared seamless VM (SSH key/known_hosts in the seamless state) and a
writable VirtioFS drive (default ``Z:``). See ``README.md`` in this directory
for the methodology and the expected output.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from vemu.errors import VemuError
from vemu.guest.ssh import SSHTransport
from vemu.seamless.launch import launch_in_session
from vemu.seamless.prepare import state_dir

HERE = Path(__file__).resolve().parent
REMOTE_DIR = "C:/vemu/soak"
REMOTE_PS = r"C:/vemu/soak/soak.ps1"
REMOTE_CMD = r"C:/vemu/soak/run-soak.cmd"


def _state(domain: str) -> dict:
    path = Path(state_dir()) / f"{domain}.json"
    if not path.is_file():
        raise VemuError(
            f"No seamless state for '{domain}' at {path}. "
            "Run 'vemu seamless prepare' first."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("host", "username", "key_path", "known_hosts"):
        if not data.get(key):
            raise VemuError(
                f"Seamless state for '{domain}' has no {key}; "
                "run 'vemu seamless prepare' again."
            )
    return data


def _prepare_remote(ssh: SSHTransport, username: str) -> None:
    script = (
        f"New-Item -ItemType Directory -Force -Path '{REMOTE_DIR}' | Out-Null; "
        f"icacls '{REMOTE_DIR}' /grant '{username}:(OI)(CI)F' | Out-Null"
    )
    result = ssh.run_powershell(script, timeout=60)
    if not result.ok:
        raise VemuError(
            "Cannot prepare the guest soak directory: " + result.output
        )


def _wait_done(
    ssh: SSHTransport,
    *,
    task_name: str,
    timeout: float,
    interval: float,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = ssh.run_powershell(
            "Test-Path 'C:\\vemu\\soak\\soak.done'", timeout=30
        )
        if result.stdout.strip().casefold() == "true":
            return
        info = ssh.run_powershell(
            f"$t = Get-ScheduledTask -TaskName '{task_name}' "
            "-ErrorAction SilentlyContinue; "
            "if ($null -eq $t) { 'missing|0' } else { "
            f"$t.State.ToString() + '|' + (Get-ScheduledTaskInfo "
            f"-TaskName '{task_name}').LastTaskResult }}",
            timeout=30,
        )
        try:
            state, code = info.stdout.strip().split("|", 1)
            code_value = int(code)
        except (ValueError, TypeError):
            state, code_value = "", 0
        state = state.casefold()
        if state == "missing":
            raise VemuError(
                "Soak task disappeared before writing soak.done. "
                "Check C:\\vemu\\soak\\soak-task-*.log."
            )
        if (
            state not in {"running", "queued", "ready"}
            and code_value != 0
        ):
            raise VemuError(
                "Soak task failed before writing soak.done "
                f"(state={state}, result={code_value}). "
                "Check C:\\vemu\\soak\\soak-task-*.log."
            )
        if state == "ready" and code_value not in {0, 267009, 267011}:
            raise VemuError(
                "Soak task failed before writing soak.done "
                f"(result={code_value}). "
                "Check C:\\vemu\\soak\\soak-task-*.log."
            )
        time.sleep(interval)
    raise VemuError(
        f"Soak test did not finish within {timeout:.0f}s. "
        "Check C:\\vemu\\soak\\soak-task-*.log and C:\\vemu\\soak\\<stamp>."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Windows seamless soak workload."
    )
    parser.add_argument("domain")
    parser.add_argument("--minutes", type=int, default=18)
    parser.add_argument("--rows", type=int, default=20000)
    parser.add_argument("--printer", default="CUPS PDF")
    parser.add_argument("--share-drive", default="Z:")
    parser.add_argument("--state-dir", help="Override the seamless state root.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800,
        help="Seconds to wait for the workload (default: 1800).",
    )
    parser.add_argument(
        "--interval", type=float, default=20, help="Poll interval in seconds."
    )
    args = parser.parse_args(argv)
    if args.state_dir:
        # state_dir() is read by _state(); keep one code path by setting the
        # documented environment override used by the seamless CLI/tests.
        import os

        os.environ["VEMU_SEAMLESS_STATE_DIR"] = args.state_dir

    try:
        state = _state(args.domain)
        ssh = SSHTransport(
            str(state["host"]),
            str(state["username"]),
            Path(str(state["key_path"])).expanduser(),
            Path(str(state["known_hosts"])).expanduser(),
            connect_timeout=20,
        )
        session = ssh.run("query user", timeout=30)
        if "active" not in session.stdout.casefold():
            raise VemuError(
                "No active Windows desktop/RAIL session. Launch one published "
                "app first (for example 'vemu seamless launch-app "
                f"{args.domain} Excel'), then rerun the soak."
            )
        share = ssh.run_powershell(
            f"Test-Path '{args.share_drive}\\'", timeout=30
        )
        if share.stdout.strip().casefold() != "true":
            raise VemuError(
                f"VirtioFS drive '{args.share_drive}' is not available in the "
                "guest. Mount the share first (for example Downloads as Z:)."
            )
        _prepare_remote(ssh, str(state["username"]))
        for name in ("soak.ps1", "run-soak.cmd", "stress.html"):
            ssh.upload((HERE / name).read_bytes(), f"{REMOTE_DIR}/{name}")
        ssh.run_powershell(
            "Remove-Item -Force -ErrorAction SilentlyContinue "
            "'C:\\vemu\\soak\\soak.done','C:\\vemu\\soak\\latest-summary.json'",
            timeout=30,
        )
        arguments = (
            f"-Minutes {args.minutes} -Rows {args.rows} "
            f'-Printer "{args.printer}" -ShareDrive "{args.share_drive}"'
        )
        task = launch_in_session(
            ssh,
            str(state["username"]),
            f"vemu-soak-{int(time.time())}",
            r"C:\Windows\System32\cmd.exe",
            f"/c {REMOTE_CMD} {arguments}",
        )
        print(f"Started soak task {task['task']} for {args.domain}")
        print("Waiting for soak.done (Ctrl-C leaves the guest task running).")
        _wait_done(
            ssh,
            task_name=str(task["task"]),
            timeout=args.timeout,
            interval=args.interval,
        )
        summary = ssh.run_powershell(
            "Get-Content 'C:\\vemu\\soak\\latest-summary.json' -Raw",
            timeout=30,
        )
        print(summary.stdout.strip())
        return 0
    except (VemuError, OSError, KeyError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
