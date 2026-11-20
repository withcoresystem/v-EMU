"""Small Windows guest helpers shared by seamless and future 3D flows."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..errors import VemuError
from .transport import CommandResult, GuestTransport

SEAMLESS_EDITIONS = {
    "Professional",
    "Enterprise",
    "Education",
    "ProfessionalWorkstation",
    "ProfessionalEducation",
}


@dataclass(frozen=True)
class WindowsInfo:
    product_name: str
    display_version: str
    build: str
    ubr: int
    edition_id: str
    installation_type: str
    hostname: str
    manufacturer: str
    model: str

    @property
    def is_windows_11(self) -> bool:
        return self.build.isdigit() and int(self.build) >= 22000

    @property
    def supports_seamless(self) -> bool:
        return self.edition_id in SEAMLESS_EDITIONS


def _json(result: CommandResult) -> Any:
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise VemuError(
            f"Windows returned invalid JSON: {result.stdout[:300]}"
        ) from exc


def query_windows_info(transport: GuestTransport) -> WindowsInfo:
    script = r"""
$ErrorActionPreference = 'Stop'
$os = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$cs = Get-CimInstance Win32_ComputerSystem
[pscustomobject]@{
  product_name = [string]$os.ProductName
  display_version = [string]$os.DisplayVersion
  build = [string]$os.CurrentBuild
  ubr = [int]$os.UBR
  edition_id = [string]$os.EditionID
  installation_type = [string]$os.InstallationType
  hostname = [string]$env:COMPUTERNAME
  manufacturer = [string]$cs.Manufacturer
  model = [string]$cs.Model
} | ConvertTo-Json -Compress
"""
    result = transport.run_powershell(script, timeout=30)
    if not result.ok:
        raise VemuError(f"Cannot query Windows version: {result.output}")
    data = _json(result)
    if not isinstance(data, dict):
        raise VemuError("Windows version query returned an invalid object.")
    return WindowsInfo(
        product_name=str(data.get("product_name", "")),
        display_version=str(data.get("display_version", "")),
        build=str(data.get("build", "")),
        ubr=int(data.get("ubr") or 0),
        edition_id=str(data.get("edition_id", "")),
        installation_type=str(data.get("installation_type", "")),
        hostname=str(data.get("hostname", "")),
        manufacturer=str(data.get("manufacturer", "")),
        model=str(data.get("model", "")),
    )


def query_services(
    transport: GuestTransport,
    names: list[str],
) -> dict[str, dict[str, Any]]:
    quoted = ",".join("'" + name.replace("'", "''") + "'" for name in names)
    script = f"""
$out = @{{}}
foreach ($name in @({quoted})) {{
  $service = Get-Service -Name $name -ErrorAction SilentlyContinue
  if ($null -eq $service) {{
    $out[$name] = [pscustomobject]@{{ present = $false; status = 'Missing'; start_type = '' }}
  }} else {{
    $out[$name] = [pscustomobject]@{{
      present = $true
      status = [string]$service.Status
      start_type = [string]$service.StartType
    }}
  }}
}}
$out | ConvertTo-Json -Depth 4 -Compress
"""
    result = transport.run_powershell(script, timeout=30)
    if not result.ok:
        raise VemuError(f"Cannot query Windows services: {result.output}")
    data = _json(result)
    if not isinstance(data, dict):
        raise VemuError("Windows service query returned an invalid object.")
    return {
        name: value
        for name, value in data.items()
        if isinstance(value, dict)
    }


def set_registry_dword(
    transport: GuestTransport,
    path: str,
    name: str,
    value: int,
) -> CommandResult:
    script = f"""
$ErrorActionPreference = 'Stop'
$path = '{path.replace("'", "''")}'
$name = '{name.replace("'", "''")}'
if (-not (Test-Path $path)) {{ New-Item -Path $path -Force | Out-Null }}
Set-ItemProperty -Path $path -Name $name -Value {int(value)} -Type DWord
"""
    return transport.run_powershell(script, timeout=30)


def set_service(
    transport: GuestTransport,
    name: str,
    *,
    startup: str | None = None,
    start: bool = False,
) -> CommandResult:
    safe_name = name.replace("'", "''")
    lines = ["$ErrorActionPreference = 'Stop'"]
    if startup:
        lines.append(
            f"Set-Service -Name '{safe_name}' -StartupType {startup}"
        )
    if start:
        lines.append(f"Start-Service -Name '{safe_name}'")
    lines.append(f"Get-Service -Name '{safe_name}' | Select-Object Name,Status,StartType | ConvertTo-Json -Compress")
    return transport.run_powershell("\n".join(lines), timeout=60)


def restart_service(
    transport: GuestTransport,
    name: str,
) -> CommandResult:
    safe_name = name.replace("'", "''")
    script = f"""
$ErrorActionPreference = 'Stop'
Restart-Service -Name '{safe_name}' -Force
Get-Service -Name '{safe_name}' | Select-Object Name,Status,StartType | ConvertTo-Json -Compress
"""
    return transport.run_powershell(script, timeout=60)


def ensure_service(
    transport: GuestTransport,
    name: str,
    *,
    startup: str = "Automatic",
    start: bool = True,
) -> dict[str, Any]:
    """Set a Windows service startup type and start it when useful.

    Returns a report instead of raising for a missing/disabled service so the
    prepare pipeline can keep going and record a warning.
    """
    safe_name = name.replace("'", "''")
    safe_startup = startup.replace("'", "''")
    start_line = (
        "Start-Service -Name $name -ErrorAction Stop"
        if start
        else ""
    )
    script = f"""
$name = '{safe_name}'
$report = [ordered]@{{
  name = $name
  present = $false
  status = ''
  start_type = ''
  error = ''
}}
$service = Get-Service -Name $name -ErrorAction SilentlyContinue
if ($null -eq $service) {{
  $report | ConvertTo-Json -Compress
  return
}}
$report.present = $true
try {{
  Set-Service -Name $name -StartupType '{safe_startup}' -ErrorAction Stop
  {start_line}
}} catch {{
  $report.error = $_.Exception.Message
}}
$now = Get-Service -Name $name -ErrorAction SilentlyContinue
if ($null -ne $now) {{
  $report.status = [string]$now.Status
  $report.start_type = [string]$now.StartType
}}
$report | ConvertTo-Json -Compress
"""
    result = transport.run_powershell(script, timeout=60)
    if not result.ok:
        return {
            "name": name,
            "present": False,
            "status": "",
            "start_type": "",
            "error": result.output.strip(),
        }
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {
            "name": name,
            "present": False,
            "status": "",
            "start_type": "",
            "error": "invalid service JSON",
        }
    return data if isinstance(data, dict) else {}


def pending_reboot(transport: GuestTransport) -> bool:
    """Return True when Windows has a pending servicing/WU reboot."""
    script = r"""
$pending = $false
$keys = @(
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired',
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending'
)
foreach ($key in $keys) {
  if (Test-Path -LiteralPath $key) { $pending = $true }
}
$rename = Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager' `
  -Name PendingFileRenameOperations -ErrorAction SilentlyContinue
if ($null -ne $rename) { $pending = $true }
if ($pending) { 'yes' } else { 'no' }
"""
    result = transport.run_powershell(script, timeout=30)
    return result.ok and result.output.strip().lower() == "yes"
