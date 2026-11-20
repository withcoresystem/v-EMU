"""One-shot FreeRDP RAIL compatibility test.

The long-lived supervisor will reuse this argument builder.  Passwords are
written to FreeRDP through stdin (``/args-from:stdin``), never argv or a
world-readable file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..errors import VemuError
from ..guest.transport import GuestTransport

WSCRIPT = r"C:\Windows\System32\wscript.exe"
SEED_SCRIPT = r"C:\ProgramData\vEMU\guest\vemu-rail-seed.vbs"
SEED_SCRIPT_UPLOAD = "C:/ProgramData/vEMU/guest/vemu-rail-seed.vbs"
SEED_VBS = "Do\r\n  WScript.Sleep 60000\r\nLoop\r\n"
FREERDP_ENV = "VEMU_XFREERDP"
FREERDP_MIN_VERSION = (3, 26, 0)
FREERDP_ICON_VERSION = (3, 32, 1)
FREERDP_RECOMMENDED_VERSION = (3, 33, 1)
_VERSION_RE = re.compile(
    r"FreeRDP version\s+(\d+)\.(\d+)\.(\d+)", re.IGNORECASE
)


@dataclass(frozen=True)
class FreeRdpStatus:
    """Host FreeRDP client capability and version details."""

    binary: str
    version: tuple[int, int, int] | None
    level: str
    message: str

    def as_dict(self) -> dict[str, object]:
        return {
            "binary": self.binary,
            "version": _version_text(self.version),
            "level": self.level,
            "message": self.message,
        }


def _version_text(version: tuple[int, int, int] | None) -> str:
    if version is None:
        return ""
    return ".".join(str(part) for part in version)


def freerdp_binary() -> str:
    """Return the xfreerdp/xfreerdp3 binary, honoring the override."""
    override = os.environ.get(FREERDP_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise VemuError(
                f"{FREERDP_ENV} does not point to an executable: {override}"
            )
        return str(path)
    # Debian/Ubuntu may package FreeRDP 3 as xfreerdp3, while Fedora,
    # RHEL-family and openSUSE still provide the xfreerdp name.
    for name in ("xfreerdp", "xfreerdp3"):
        binary = shutil.which(name)
        if binary:
            return binary
    raise VemuError(
        "xfreerdp or xfreerdp3 was not found; install FreeRDP 3."
    )


def _freerdp_environment() -> dict[str, str]:
    """Return an environment where FreeRDP can find its home directory.

    The root daemon starts without ``HOME`` in some systemd configurations;
    ``xfreerdp /version`` then exits with a KNOWN_PATH_HOME warning and no
    version string, making every seamless status say "unverified".
    """
    env = os.environ.copy()
    if not env.get("HOME"):
        env["HOME"] = os.path.expanduser("~") or tempfile.gettempdir()
    return env


def freerdp_version(binary: str | None = None) -> tuple[int, int, int] | None:
    """Return the installed FreeRDP version, or None when it is unknown."""
    executable = binary or freerdp_binary()
    try:
        result = subprocess.run(
            [executable, "/version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=_freerdp_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _VERSION_RE.search(
        f"{result.stdout}\n{result.stderr}"
    )
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def freerdp_status(binary: str | None = None) -> FreeRdpStatus:
    """Classify the host client against the tested RAIL versions."""
    executable = binary or freerdp_binary()
    version = freerdp_version(executable)
    if version is None:
        return FreeRdpStatus(
            executable,
            None,
            "unknown",
            "Could not determine the FreeRDP version; RAIL support is "
            "unverified.",
        )
    text = _version_text(version)
    if version < FREERDP_MIN_VERSION:
        return FreeRdpStatus(
            executable,
            version,
            "unsupported",
            f"FreeRDP {text} is too old for RemoteApp/RAIL; install FreeRDP "
            f">= {_version_text(FREERDP_MIN_VERSION)} or use the Flatpak "
            "3.33.1 client via VEMU_XFREERDP.",
        )
    if version < FREERDP_ICON_VERSION:
        return FreeRdpStatus(
            executable,
            version,
            "warn",
            f"FreeRDP {text} works for RAIL core but lacks the icon-alpha "
            f"and resize fixes; FreeRDP >= "
            f"{_version_text(FREERDP_RECOMMENDED_VERSION)} is recommended.",
        )
    return FreeRdpStatus(
        executable,
        version,
        "ok",
        f"FreeRDP {text}",
    )


def ensure_freerdp_compatible(
    binary: str | None = None,
) -> FreeRdpStatus:
    """Return client status and reject versions known to break RAIL."""
    status = freerdp_status(binary)
    if status.level == "unsupported":
        raise VemuError(status.message)
    return status


ERROR_MARKERS = {
    "RAIL_EXEC_E_NOT_IN_ALLOWLIST": "the program is not in the RemoteApp allowlist",
    "RAIL_EXEC_E_": "the RemoteApp launcher rejected the program",
    "ERRCONNECT_": "the RDP connection failed",
    "LOGON_FAILURE": "Windows rejected the credentials",
    "SEC_E_LOGON_DENIED": "Windows rejected the credentials",
    "Authentication failure": "Windows rejected the credentials",
}


@dataclass(frozen=True)
class RailTest:
    ok: bool
    connected: bool
    reason: str
    log: str
    exitcode: int | None = None
    elapsed: float = 0.0


def build_arguments(
    host: str,
    username: str,
    password: str,
    program: str,
    arguments: str = "",
    *,
    size: str = "1280x800",
    cert: str = "ignore",
    drive: str | None = None,
    reconnect: bool = False,
    audio: bool = True,
    audio_latency: int = 200,
    audio_quality: str = "dynamic",
    multimon: bool = False,
    grab_input: bool = False,
) -> list[str]:
    """Return one ``/args-from:stdin`` line per FreeRDP argument."""
    if any(character in password for character in "\r\n"):
        raise VemuError("The RDP password must not contain a line break.")
    if not program.strip():
        raise VemuError("A RemoteApp program path is required.")
    lines = [
        f"/v:{host}",
        f"/u:{username}",
        f"/p:{password}",
        f"/cert:{cert}",
        "/sec:nla",
        f"/size:{size}",
    ]
    if drive:
        lines.append(f"/drive:{drive}")
    if not grab_input:
        # RAIL windows do not need a global grab; leaving it on can trap the
        # host pointer after the last remote window closes.
        lines.extend(
            ["-grab-keyboard", "-grab-mouse", "/mouse:grab:off"]
        )
    if audio:
        sound: list[str] = []
        if audio_latency > 0:
            sound.append(f"latency:{int(audio_latency)}")
        if audio_quality:
            sound.append(f"quality:{audio_quality}")
        lines.append("/sound:" + ",".join(sound) if sound else "/sound")
    if multimon:
        lines.append("+multimon")
    if reconnect:
        lines.extend(
            [
                "+auto-reconnect",
                "/auto-reconnect-max-retries:20",
            ]
        )
    app = f"/app:program:{program.strip()}"
    if arguments.strip():
        app += f",cmd:{arguments.strip()}"
    lines.append(app)
    return lines


def _tail(path: Path, limit: int = 40) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-limit:])


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass


def test_rail_app(
    host: str,
    username: str,
    password: str,
    program: str,
    arguments: str = "",
    *,
    size: str = "1280x800",
    timeout: float = 30.0,
    settle: float = 8.0,
    display: str | None = None,
    cert: str = "ignore",
    multimon: bool = False,
    audio_latency: int = 200,
    audio_quality: str = "dynamic",
) -> RailTest:
    """Open one RAIL app, keep it for *settle* seconds, then close it."""
    status = ensure_freerdp_compatible()
    binary = status.binary
    lines = build_arguments(
        host,
        username,
        password,
        program,
        arguments,
        size=size,
        cert=cert,
        multimon=multimon,
        audio_latency=audio_latency,
        audio_quality=audio_quality,
    )
    environment = os.environ.copy()
    if display:
        environment["DISPLAY"] = display
    with tempfile.TemporaryDirectory(prefix="vemu-rail-") as tmp:
        log_path = Path(tmp) / "xfreerdp.log"
        started = time.monotonic()
        with log_path.open("wb") as log:
            process = subprocess.Popen(
                [binary, "/args-from:stdin"],
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=environment,
                start_new_session=True,
            )
            try:
                if process.stdin is not None:
                    process.stdin.write(
                        ("\n".join(lines) + "\n").encode("utf-8")
                    )
                    process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            result: RailTest
            while True:
                elapsed = time.monotonic() - started
                text = _tail(log_path, 80)
                lowered = text.casefold()
                matched = next(
                    (
                        marker
                        for marker in ERROR_MARKERS
                        if marker.casefold() in lowered
                    ),
                    None,
                )
                if matched:
                    _stop(process)
                    result = RailTest(
                        ok=False,
                        connected=False,
                        reason=ERROR_MARKERS[matched],
                        log=_redact(text, password),
                        exitcode=process.poll(),
                        elapsed=elapsed,
                    )
                    break
                code = process.poll()
                if code is not None:
                    result = RailTest(
                        ok=False,
                        connected=False,
                        reason=(
                            "xfreerdp exited before the RAIL window settled"
                        ),
                        log=_redact(text, password),
                        exitcode=code,
                        elapsed=elapsed,
                    )
                    break
                if elapsed >= settle:
                    _stop(process)
                    result = RailTest(
                        ok=True,
                        connected=True,
                        reason="RAIL window stayed connected",
                        log=_redact(text, password),
                        exitcode=process.poll(),
                        elapsed=elapsed,
                    )
                    break
                if elapsed >= timeout:
                    _stop(process)
                    result = RailTest(
                        ok=False,
                        connected=False,
                        reason="timed out waiting for the RAIL window",
                        log=_redact(text, password),
                        exitcode=process.poll(),
                        elapsed=elapsed,
                    )
                    break
                time.sleep(0.2)
        return result


def _redact(text: str, password: str) -> str:
    if password:
        return text.replace(password, "***")
    return text


def _escape_powershell(value: str) -> str:
    return value.replace("'", "''")


def launch_in_session(
    transport: GuestTransport,
    username: str,
    name: str,
    path: str,
    arguments: str = "",
) -> dict[str, str]:
    """Start an app in the existing interactive session via Task Scheduler."""
    if not path.strip():
        raise VemuError("A program path is required.")
    digest = hashlib.sha1(
        f"{name}|{path}".encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:12]
    task_name = f"vemu-launch-{digest}"
    safe_path = _escape_powershell(path.strip())
    safe_args = _escape_powershell(arguments.strip())
    safe_user = _escape_powershell(username.strip())
    safe_task = _escape_powershell(task_name)
    argument_line = (
        f" -Argument '{safe_args}'" if safe_args else ""
    )
    script = f"""
$ErrorActionPreference = 'Stop'
$action = New-ScheduledTaskAction -Execute '{safe_path}'{argument_line}
$principal = New-ScheduledTaskPrincipal -UserId '{safe_user}' `
  -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName '{safe_task}' -Action $action `
  -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName '{safe_task}'
Start-Sleep -Milliseconds 800
$state = (Get-ScheduledTask -TaskName '{safe_task}').State
[pscustomobject]@{{ task = '{safe_task}'; state = [string]$state }} |
  ConvertTo-Json -Compress
"""
    result = transport.run_powershell(script, timeout=60)
    if not result.ok:
        raise VemuError(
            f"Cannot launch '{name}' in the Windows session: {result.output}"
        )
    return {"task": task_name, "output": result.output.strip()}


def stop_seed(transport: GuestTransport) -> int:
    """Stop leftover windowless RAIL seed processes."""
    script = r"""
$count = 0
Get-CimInstance Win32_Process -Filter "Name='wscript.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match 'vemu-rail-seed\.vbs' } |
  ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    $count++
  }
$count
"""
    result = transport.run_powershell(script, timeout=30)
    if not result.ok:
        return 0
    try:
        return int(result.output.strip() or "0")
    except ValueError:
        return 0


def ensure_seed(transport: GuestTransport) -> dict[str, str]:
    """Deploy a windowless Windows process that keeps the RAIL pipe open."""
    stop_seed(transport)
    script = f"""
$ErrorActionPreference = 'Stop'
$dir = 'C:/ProgramData/vEMU/guest'
New-Item -ItemType Directory -Path $dir -Force | Out-Null
"""
    result = transport.run_powershell(script, timeout=30)
    if not result.ok:
        raise VemuError(f"Cannot prepare the RAIL seed directory: {result.output}")
    transport.upload(SEED_VBS.encode("utf-8"), SEED_SCRIPT_UPLOAD)
    from .rdp import add_remoteapp_app

    add_remoteapp_app(
        transport,
        "vEMU RAIL host",
        WSCRIPT,
        required_command_line=SEED_SCRIPT,
    )
    return {"program": WSCRIPT, "arguments": SEED_SCRIPT}


def wait_for_session(
    transport: GuestTransport,
    username: str,
    *,
    timeout: float = 20.0,
) -> bool:
    """Wait until the new RDP session is active enough for a task launch."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = transport.run_powershell("quser 2>&1", timeout=20)
        text = result.output.casefold()
        if "active" in text and username.casefold() in text:
            return True
        time.sleep(0.75)
    return False


def supervisor_paths(state_dir: str | Path, domain: str) -> tuple[Path, Path]:
    base = Path(state_dir).expanduser()
    return (
        base / f"{domain}.rail.pid",
        base / f"{domain}.rail.log",
    )


def supervisor_pid(pid_path: Path) -> int | None:
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    try:
        command = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if b"xfreerdp" not in command:
        return None
    return pid


def start_supervisor(
    host: str,
    username: str,
    password: str,
    program: str,
    arguments: str = "",
    *,
    display: str | None = None,
    log_path: Path,
    pid_path: Path,
    size: str = "1280x800",
    cert: str = "ignore",
    multimon: bool = False,
    audio_latency: int = 200,
    audio_quality: str = "dynamic",
) -> int:
    """Start the one long-lived RAIL client and return its PID."""
    status = ensure_freerdp_compatible()
    binary = status.binary
    lines = build_arguments(
        host,
        username,
        password,
        program,
        arguments,
        size=size,
        cert=cert,
        reconnect=True,
        multimon=multimon,
        audio_latency=audio_latency,
        audio_quality=audio_quality,
    )
    environment = os.environ.copy()
    if display:
        environment["DISPLAY"] = display
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [binary, "/args-from:stdin"],
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
            start_new_session=True,
        )
        if process.stdin is not None:
            process.stdin.write(
                ("\n".join(lines) + "\n").encode("utf-8")
            )
            process.stdin.close()
    finally:
        log.close()
    time.sleep(0.6)
    if process.poll() is not None:
        raise VemuError(
            "The RAIL supervisor exited during startup: "
            + _tail(log_path, 20)
        )
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(f"{process.pid}\n", encoding="utf-8")
    meta_path = pid_path.with_suffix(".meta.json")
    safe_args = [
        "/p:***" if line.startswith("/p:") else line
        for line in lines
    ]
    meta_path.write_text(
        json.dumps(
            {
                "pid": process.pid,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "binary": binary,
                "grab_keyboard": "-grab-keyboard" not in lines,
                "grab_mouse": "-grab-mouse" not in lines,
                "args": safe_args,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(meta_path, 0o600)
    except OSError:
        pass
    try:
        os.chmod(pid_path, 0o600)
    except OSError:
        pass
    return process.pid


def stop_supervisor(pid_path: Path) -> bool:
    pid = supervisor_pid(pid_path)
    if pid is None:
        try:
            pid_path.unlink()
        except OSError:
            pass
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        pid_path.unlink()
    except OSError:
        pass
    return True
