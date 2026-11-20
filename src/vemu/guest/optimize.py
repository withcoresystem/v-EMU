"""Apply the shared Windows guest optimization profile.

The PowerShell helper and its profile data stay in ``contrib/windows`` so they
remain usable outside v-EMU.  This module uploads the same files, runs the
helper in the guest and verifies the result.  It never forks the profile data.
"""

from __future__ import annotations

import json
import os
import re
import sysconfig
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import VemuError
from .transport import GuestTransport
from .windows import query_services

PROFILES = ("Safe", "Balanced", "3D")
DEFAULT_PROFILE = "Safe"
REMOTE_DIR = r"C:\ProgramData\vEMU\guest"
REMOTE_SCRIPT = REMOTE_DIR + r"\vemu-guest-optimize.ps1"
REMOTE_PROFILES = REMOTE_DIR + r"\profiles.json"
REMOTE_LOG = REMOTE_DIR + r"\vemu-guest-optimize.log"
HIGH_PERFORMANCE = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"

# Services that must remain up after the profile unless they were absent or
# already stopped.  The full protected list is intentional for the PowerShell
# guard, but many Windows services are trigger-start and may legitimately stop
# between two samples (StateRepository is a common example).
REQUIRED_SERVICES = {
    "QEMU-GA",
    "sshd",
    "TermService",
    "WinDefend",
    "mpssvc",
    "BFE",
    "BalloonService",
    "VioGpuSrv",
    "WinFsp",
    "viofs",
    "VirtioFsSvc",
    "vioserial",
}

ASSET_NAMES = ("vemu-guest-optimize.ps1", "profiles.json")

_POWER_AC = re.compile(
    r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", re.MULTILINE
)
_POWER_DC = re.compile(
    r"Current DC Power Setting Index:\s*0x([0-9a-fA-F]+)", re.MULTILINE
)


@dataclass(frozen=True)
class OptimizationReport:
    """Outcome of one optimization profile application."""

    profile: str
    exitcode: int
    log: str
    state: dict[str, Any]
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return self.exitcode == 0 and not self.problems


def _candidate_dirs() -> list[Path]:
    paths: list[Path] = []
    override = os.environ.get("VEMU_WINDOWS_ASSETS")
    if override:
        paths.append(Path(override).expanduser())
    # Source checkout: src/vemu/guest/optimize.py -> repository root.
    paths.append(Path(__file__).resolve().parents[3] / "contrib" / "windows")
    # pip --target keeps data-files under the target site-packages directory,
    # which is how setup.sh installs the daemon copy.
    paths.append(
        Path(__file__).resolve().parents[2] / "share" / "vemu" / "windows"
    )
    schemes = sysconfig.get_paths()
    for key in ("data", "platdata"):
        prefix = schemes.get(key)
        if prefix:
            paths.append(Path(prefix) / "share" / "vemu" / "windows")
    paths.append(Path("/usr/share/vemu/windows"))
    paths.append(Path("/usr/local/share/vemu/windows"))
    seen: set[Path] = set()
    unique: list[Path] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def assets_dir() -> Path:
    """Return the directory holding the standalone helper and profiles."""
    for directory in _candidate_dirs():
        if all((directory / name).is_file() for name in ASSET_NAMES):
            return directory
    raise VemuError(
        "The Windows guest optimization assets were not found. Expected "
        "vemu-guest-optimize.ps1 and profiles.json in contrib/windows or "
        "<prefix>/share/vemu/windows."
    )


def _load_profile(profile: str) -> dict[str, Any]:
    if profile not in PROFILES:
        raise VemuError(
            f"Unknown optimization profile '{profile}'; expected one of "
            + ", ".join(PROFILES)
        )
    directory = assets_dir()
    try:
        data = json.loads(
            (directory / "profiles.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise VemuError(f"Cannot read the Windows profiles: {exc}") from exc
    if not isinstance(data, dict):
        raise VemuError("The Windows profile data is not a JSON object.")
    if profile not in data.get("profiles", {}):
        raise VemuError(f"Profile '{profile}' is missing from profiles.json.")
    return data


def protected_services(profile: str = DEFAULT_PROFILE) -> list[str]:
    """Return the explicit protected service names from the shared data."""
    data = _load_profile(profile)
    protected = data.get("protected", {})
    names = protected.get("services", []) if isinstance(protected, dict) else []
    return [str(name) for name in names if str(name).strip()]


def _ensure_remote_dir(transport: GuestTransport) -> None:
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        f"New-Item -ItemType Directory -Path '{REMOTE_DIR}' -Force | Out-Null\n"
    )
    result = transport.run_powershell(script, timeout=30)
    if not result.ok:
        raise VemuError(f"Cannot prepare {REMOTE_DIR}: {result.output}")


def _parse_indexes(output: str) -> dict[str, int] | None:
    ac = _POWER_AC.search(output)
    dc = _POWER_DC.search(output)
    if not ac or not dc:
        return None
    return {"ac": int(ac.group(1), 16), "dc": int(dc.group(1), 16)}


def _power_setting(
    transport: GuestTransport,
    subgroup: str,
    setting: str,
) -> dict[str, int] | None:
    result = transport.run(
        r"C:\Windows\System32\powercfg.exe",
        ["/query", "SCHEME_CURRENT", subgroup, setting],
        timeout=30,
    )
    if not result.ok:
        return None
    return _parse_indexes(result.output)


def _registry_dword(transport: GuestTransport, path: str, name: str) -> int | None:
    result = transport.run(
        r"C:\Windows\System32\reg.exe",
        ["query", path, "/v", name],
        timeout=20,
    )
    if not result.ok:
        return None
    match = re.search(
        r"REG_DWORD\s+0x([0-9a-fA-F]+)", result.output, re.MULTILINE
    )
    if not match:
        return None
    return int(match.group(1), 16)


def collect_state(
    transport: GuestTransport,
    *,
    profile: str = DEFAULT_PROFILE,
    services: list[str] | None = None,
) -> dict[str, Any]:
    """Read the power, startup and protected-service state from the guest."""
    scheme = transport.run(
        r"C:\Windows\System32\powercfg.exe",
        ["/getactivescheme"],
        timeout=30,
    )
    guid = ""
    name = ""
    match = re.search(r"([0-9a-fA-F-]{36})\s+\((.*?)\)", scheme.output)
    if match:
        guid = match.group(1).lower()
        name = match.group(2).strip()
    names = services if services is not None else protected_services(profile)
    return {
        "active_scheme": {"guid": guid, "name": name},
        "hiberboot": _registry_dword(
            transport,
            r"HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Power",
            "HiberbootEnabled",
        ),
        "hibernate_enabled": _registry_dword(
            transport,
            r"HKLM\SYSTEM\CurrentControlSet\Control\Power",
            "HibernateEnabled",
        ),
        "standby_timeout": _power_setting(
            transport,
            "238c9fa8-0aad-41ed-83f4-97be242c8f20",
            "29f6c1db-86da-48c5-9fdb-f2b67b1f44da",
        ),
        "usb_selective_suspend": _power_setting(
            transport,
            "2a737441-1930-4402-8d77-b2bebba308a3",
            "48e6b7a6-50f5-4782-a5d4-53bb8f07e226",
        ),
        "pcie_aspm": _power_setting(
            transport,
            "501a4d13-42af-4429-9fd1-a8218c268e20",
            "ee12f906-d277-404b-b6da-e5fa1a576df5",
        ),
        "services": query_services(transport, names) if names else {},
    }


def _check_state(
    state: dict[str, Any],
    *,
    baseline: dict[str, dict[str, Any]] | None,
) -> list[str]:
    problems: list[str] = []
    scheme = state.get("active_scheme", {})
    if not isinstance(scheme, dict) or scheme.get("guid") != HIGH_PERFORMANCE:
        problems.append(
            "High performance power plan is not active "
            f"({scheme.get('guid') or 'unknown'})."
        )
    if state.get("hiberboot") not in (0, None):
        problems.append("Fast Startup (HiberbootEnabled) is still enabled.")
    if state.get("hibernate_enabled") not in (0, None):
        problems.append("Hibernation is still enabled.")
    for key, label in (
        ("standby_timeout", "Standby timeout"),
        ("usb_selective_suspend", "USB selective suspend"),
        ("pcie_aspm", "PCIe ASPM"),
    ):
        value = state.get(key)
        if not isinstance(value, dict):
            continue
        for mode in ("ac", "dc"):
            if value.get(mode) not in (0, None):
                problems.append(f"{label} is still enabled on {mode.upper()}.")
    if baseline:
        current = state.get("services", {})
        for name, before in baseline.items():
            if name not in REQUIRED_SERVICES:
                continue
            if before.get("status") != "Running":
                continue
            now = current.get(name, {})
            if now.get("status") != "Running":
                problems.append(
                    f"Protected service '{name}' was running and is now "
                    f"{now.get('status') or 'missing'}."
                )
    return problems


def apply_profile(
    transport: GuestTransport,
    profile: str = DEFAULT_PROFILE,
    *,
    user_hive: str | None = None,
    skip_power: bool = False,
    skip_user_settings: bool = False,
    dry_run: bool = False,
    verify: bool = True,
    timeout: int = 900,
    strict: bool = True,
) -> OptimizationReport:
    """Upload and run the standalone helper, then verify the guest."""
    data = _load_profile(profile)
    directory = assets_dir()
    service_names = [
        str(name)
        for name in data.get("protected", {}).get("services", [])
        if str(name).strip()
    ]
    baseline = (
        query_services(transport, service_names) if service_names else {}
    )

    _ensure_remote_dir(transport)
    transport.upload(
        (directory / "vemu-guest-optimize.ps1").read_bytes(), REMOTE_SCRIPT
    )
    transport.upload((directory / "profiles.json").read_bytes(), REMOTE_PROFILES)

    args = [
        "-Profile",
        profile,
        "-LogPath",
        REMOTE_LOG,
    ]
    if dry_run:
        args.append("-DryRun")
    if skip_power:
        args.append("-SkipPower")
    if skip_user_settings:
        args.append("-SkipUserSettings")
    if user_hive:
        args.extend(["-UserHive", user_hive])
    result = transport.run(
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        [
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            REMOTE_SCRIPT,
            *args,
        ],
        timeout=timeout,
    )
    try:
        log = transport.download(REMOTE_LOG).decode("utf-8", "replace")
    except VemuError:
        log = result.output
    state: dict[str, Any] = {}
    problems: list[str] = []
    if verify and not dry_run and result.ok:
        state = collect_state(
            transport, profile=profile, services=service_names
        )
        problems = _check_state(state, baseline=baseline)

    report = OptimizationReport(
        profile=profile,
        exitcode=result.exitcode if result.exitcode is not None else -1,
        log=log,
        state=state,
        problems=tuple(problems),
    )
    if strict:
        if not result.ok:
            tail = "\n".join(log.strip().splitlines()[-12:])
            raise VemuError(
                f"Windows guest optimization failed (exit {report.exitcode})"
                + (f":\n{tail}" if tail else ".")
            )
        if problems:
            raise VemuError(
                "Windows guest optimization verification failed: "
                + "; ".join(problems)
            )
    return report
