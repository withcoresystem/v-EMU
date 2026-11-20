"""RDP/RemoteApp setup for Windows seamless mode."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from ..errors import VemuError
from ..guest.transport import CommandResult, GuestTransport
from ..guest.windows import WindowsInfo, query_services, query_windows_info

RDP_ALLOWLIST = (
    r"HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    r"\Terminal Server\TSAppAllowList"
)


def _escape(value: str) -> str:
    return value.replace("'", "''")


def _json(result: CommandResult) -> Any:
    try:
        return json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise VemuError(
            f"Guest returned invalid JSON: {result.stdout[:300]}"
        ) from exc


def _query_rdp(transport: GuestTransport) -> dict[str, Any]:
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$rdp = Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' -Name fDenyTSConnections
$allow = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Terminal Server\TSAppAllowList'
$apps = Get-ChildItem 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Terminal Server\TSAppAllowList\Applications' |
  ForEach-Object { $p = Get-ItemProperty $_.PSPath; [pscustomobject]@{ name=$_.PSChildName; path=$p.Path; command_line_setting=$p.CommandLineSetting } }
$listeners = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
  Where-Object { $_.LocalPort -in 22,3389 } | Select-Object LocalAddress,LocalPort
$fw = Get-NetFirewallRule -DisplayGroup 'Remote Desktop' -ErrorAction SilentlyContinue |
  Select-Object DisplayName,Enabled,Profile,Direction,Action
[pscustomobject]@{
  fDenyTSConnections = $rdp.fDenyTSConnections
  fDisabledAllowList = $allow.fDisabledAllowList
  apps = @($apps)
  listeners = @($listeners)
  firewall = @($fw)
} | ConvertTo-Json -Depth 6 -Compress
"""
    result = transport.run_powershell(script, timeout=30)
    if not result.ok:
        raise VemuError(f"Cannot query RDP state: {result.output}")
    data = _json(result)
    if not isinstance(data, dict):
        raise VemuError("RDP state query returned an invalid object.")
    return data


def preflight(
    transport: GuestTransport,
    *,
    info: WindowsInfo | None = None,
) -> dict[str, Any]:
    """Read-only prerequisite check for the seamless helper."""
    if info is None:
        info = query_windows_info(transport)
    services = query_services(
        transport,
        [
            "QEMU-GA",
            "sshd",
            "TermService",
            "UmRdpService",
            "WinFsp.Launcher",
        ],
    )
    rdp = _query_rdp(transport)
    reasons: list[str] = []
    if not info.supports_seamless:
        reasons.append(
            f"Windows edition '{info.edition_id}' does not support an RDP host."
        )
    ssh = services.get("sshd", {})
    if not ssh.get("present") or ssh.get("status") != "Running":
        reasons.append("OpenSSH Server (sshd) is not running.")
    qga = services.get("QEMU-GA", {})
    if not qga.get("present") or qga.get("status") != "Running":
        reasons.append("QEMU guest agent is not running.")
    listener_ports = {
        int(item.get("LocalPort"))
        for item in rdp.get("listeners", [])
        if isinstance(item, dict) and item.get("LocalPort") is not None
    }
    return {
        "supported": not reasons,
        "reasons": reasons,
        "info": asdict(info),
        "services": services,
        "rdp": rdp,
        "ssh_ready": bool(
            ssh.get("present") and ssh.get("status") == "Running"
        ),
        "ssh_listening": 22 in listener_ports,
        "rdp_listening": 3389 in listener_ports,
        "winfsp": services.get("WinFsp.Launcher", {}),
        "qga": qga,
    }


def enable_rdp(transport: GuestTransport) -> dict[str, Any]:
    """Enable the RDP host, services and firewall for every profile."""
    script = r"""
$ErrorActionPreference = 'Stop'
Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' `
  -Name fDenyTSConnections -Value 0 -Type DWord
Set-Service -Name TermService -StartupType Automatic
Start-Service -Name TermService
Start-Service -Name UmRdpService
Get-NetFirewallRule -DisplayGroup 'Remote Desktop' |
  Set-NetFirewallRule -Enabled True -Profile Any
Get-Service -Name TermService,UmRdpService |
  Select-Object Name,Status,StartType | ConvertTo-Json -Compress
"""
    result = transport.run_powershell(script, timeout=90)
    if not result.ok:
        raise VemuError(f"Cannot enable RDP: {result.output}")
    return _query_rdp(transport)


ADMIN_KEYS = r"C:\ProgramData\ssh\administrators_authorized_keys"


def rdp_certificate_sha256(transport: GuestTransport) -> str | None:
    """Return the active RDP certificate SHA256 fingerprint, if present."""
    script = r"""
$cert = Get-ChildItem 'Cert:\LocalMachine\Remote Desktop' -ErrorAction SilentlyContinue |
  Sort-Object NotAfter -Descending | Select-Object -First 1
if ($null -ne $cert) { $cert.GetCertHashString('SHA256') }
"""
    result = transport.run_powershell(script, timeout=30)
    if not result.ok:
        return None
    value = result.output.strip().upper()
    if len(value) == 64 and all(c in "0123456789ABCDEF" for c in value):
        return value
    return None


def configure_ssh_key_only(transport: GuestTransport) -> dict[str, Any]:
    """Disable SSH password authentication after a key login is verified."""
    script = r"""
$ErrorActionPreference = 'Stop'
$path = 'C:\ProgramData\ssh\sshd_config'
Copy-Item -LiteralPath $path -Destination "$path.vemu.bak" -Force
$managed = @(
  'PasswordAuthentication no',
  'KbdInteractiveAuthentication no',
  'PubkeyAuthentication yes'
)
$filtered = Get-Content -LiteralPath $path |
  Where-Object {
    $_ -notmatch '^\s*(PasswordAuthentication|KbdInteractiveAuthentication|PubkeyAuthentication)\b'
  }
Set-Content -LiteralPath $path -Value ($managed + $filtered) -Encoding ASCII
Restart-Service -Name sshd -Force
Start-Sleep -Milliseconds 800
$sshd = Join-Path $env:SystemRoot 'System32\OpenSSH\sshd.exe'
& $sshd -T 2>&1 |
  Select-String -Pattern '^(passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication)' |
  ForEach-Object { $_.Line }
"""
    result = transport.run_powershell(script, timeout=60)
    if not result.ok:
        raise VemuError(
            f"Cannot enforce key-only SSH authentication: {result.output}"
        )
    return {
        "password_authentication": False,
        "keyboard_interactive": False,
        "pubkey_authentication": True,
        "effective": [
            line.strip()
            for line in result.output.splitlines()
            if line.strip()
        ],
    }


def inspect_windows_user(
    transport: GuestTransport,
    username: str,
) -> dict[str, Any]:
    """Return admin / Remote Desktop Users membership for a local user."""
    safe_user = _escape(username.strip())
    if not safe_user:
        raise VemuError("A Windows username is required.")
    script = f"""
$ErrorActionPreference = 'Stop'
$user = Get-LocalUser -Name '{safe_user}' -ErrorAction Stop
$adminGroup = Get-LocalGroup | Where-Object {{ $_.SID.Value -eq 'S-1-5-32-544' }}
$rdpGroup = Get-LocalGroup | Where-Object {{ $_.SID.Value -eq 'S-1-5-32-555' }}
$isAdmin = $false
$inRdp = $false
$profile = Get-CimInstance Win32_UserProfile -ErrorAction SilentlyContinue |
  Where-Object {{ $_.SID -eq $user.SID.Value }} | Select-Object -First 1
if ($null -ne $adminGroup) {{
  $isAdmin = @(Get-LocalGroupMember -Group $adminGroup.Name -ErrorAction SilentlyContinue |
    Where-Object {{ $_.SID.Value -eq $user.SID.Value }}).Count -gt 0
}}
if ($null -ne $rdpGroup) {{
  $inRdp = @(Get-LocalGroupMember -Group $rdpGroup.Name -ErrorAction SilentlyContinue |
    Where-Object {{ $_.SID.Value -eq $user.SID.Value }}).Count -gt 0
}}
$added = $false
if (-not $isAdmin -and -not $inRdp) {{
  if ($null -eq $rdpGroup) {{ throw 'Remote Desktop Users group not found' }}
  Add-LocalGroupMember -Group $rdpGroup.Name -Member $user.Name
  $inRdp = $true
  $added = $true
}}
[pscustomobject]@{{
  user = $user.Name
  admin = $isAdmin
  remote_desktop = $inRdp
  added_to_remote_desktop = $added
  profile = if ($null -ne $profile) {{ [string]$profile.LocalPath }} else {{ '' }}
}} | ConvertTo-Json -Compress
"""
    result = transport.run_powershell(script, timeout=45)
    if not result.ok:
        raise VemuError(
            f"Cannot inspect Windows user '{username}': {result.output}"
        )
    data = _json(result)
    if not isinstance(data, dict):
        raise VemuError("Windows user inspection returned an invalid object.")
    return data


def install_user_key(
    transport: GuestTransport,
    username: str,
    public_key: str,
    *,
    restart: bool = True,
) -> dict[str, Any]:
    """Install a key for a standard user in their per-user .ssh directory."""
    key = public_key.strip()
    if not key.startswith("ssh-") or "\n" in key:
        raise VemuError("A single OpenSSH public key line is required.")
    temporary = "C:/Windows/Temp/vemu-user-key.pub"
    transport.upload((key + "\n").encode("utf-8"), temporary)
    safe_user = _escape(username.strip())
    script = f"""
$ErrorActionPreference = 'Stop'
$user = Get-LocalUser -Name '{safe_user}' -ErrorAction Stop
$profile = Get-CimInstance Win32_UserProfile -ErrorAction SilentlyContinue |
  Where-Object {{ $_.SID -eq $user.SID }} | Select-Object -First 1
if ($null -eq $profile) {{
  throw 'Log into Windows as this user once (console or RDP), then rerun prepare so the user profile exists.'
}}
$profileDir = [string]$profile.LocalPath
$dir = Join-Path $profileDir '.ssh'
New-Item -ItemType Directory -Path $dir -Force | Out-Null
$target = Join-Path $dir 'authorized_keys'
$newKey = (Get-Content -LiteralPath 'C:/Windows/Temp/vemu-user-key.pub' -Raw).Trim()
$existing = @()
if (Test-Path -LiteralPath $target) {{
  $existing = @(Get-Content -LiteralPath $target)
}}
if ($existing -notcontains $newKey) {{
  Add-Content -LiteralPath $target -Value $newKey -Encoding ASCII
}}
icacls $target /inheritance:r /grant:r "*$($user.SID.Value):F" '*S-1-5-18:F' '*S-1-5-32-544:F' | Out-Null
icacls $target /setowner "$($user.Name)" | Out-Null
Remove-Item -LiteralPath 'C:/Windows/Temp/vemu-user-key.pub' -Force -ErrorAction SilentlyContinue
Get-Item -LiteralPath $target | Select-Object FullName,Length | ConvertTo-Json -Compress
"""
    result = transport.run_powershell(script, timeout=45)
    if not result.ok:
        raise VemuError(
            f"Cannot install the SSH key for '{username}': {result.output}"
        )
    if restart:
        result = transport.run_powershell(
            "Restart-Service -Name sshd -Force; "
            "Get-Service -Name sshd | Select-Object Name,Status | ConvertTo-Json -Compress",
            timeout=60,
        )
        if not result.ok:
            raise VemuError(f"Cannot restart sshd: {result.output}")
    return {
        "installed": True,
        "path": "per-user .ssh/authorized_keys",
        "restarted": restart,
        "user": username,
    }


def install_admin_key(
    transport: GuestTransport,
    public_key: str,
    *,
    restart: bool = True,
) -> dict[str, Any]:
    """Install one public key for Windows administrators and fix its ACL."""
    key = public_key.strip()
    if not key.startswith("ssh-") or "\n" in key:
        raise VemuError("A single OpenSSH public key line is required.")
    existing = ""
    try:
        existing = transport.download(ADMIN_KEYS).decode("utf-8", "replace")
    except VemuError:
        existing = ""
    lines = [line.strip() for line in existing.splitlines() if line.strip()]
    if key not in lines:
        lines.append(key)
    script = rf"""
$ErrorActionPreference = 'Stop'
$dir = 'C:\ProgramData\ssh'
$file = '{ADMIN_KEYS}'
New-Item -ItemType Directory -Path $dir -Force | Out-Null
icacls $file /inheritance:r /grant '*S-1-5-32-544:F' '*S-1-5-18:F' | Out-Null
"""
    transport.upload(("\n".join(lines) + "\n").encode("utf-8"), ADMIN_KEYS)
    result = transport.run_powershell(script, timeout=30)
    if not result.ok:
        raise VemuError(f"Cannot set SSH key ACL: {result.output}")
    if restart:
        result = transport.run_powershell(
            "Restart-Service -Name sshd -Force; "
            "Get-Service -Name sshd | Select-Object Name,Status | ConvertTo-Json -Compress",
            timeout=60,
        )
        if not result.ok:
            raise VemuError(f"Cannot restart sshd: {result.output}")
    return {"installed": True, "path": ADMIN_KEYS, "restarted": restart}


def add_remoteapp_app(
    transport: GuestTransport,
    name: str,
    path: str,
    *,
    command_line: bool = True,
    required_command_line: str = "",
) -> dict[str, Any]:
    """Add one application to the machine-wide RemoteApp allowlist."""
    if not name.strip() or not path.strip():
        raise VemuError("RemoteApp name and path are required.")
    safe_name = _escape(name.strip())
    safe_path = _escape(path.strip())
    safe_key = _escape(name.strip().replace("\\", "-"))
    safe_args = _escape(required_command_line.strip())
    command_value = 1 if command_line or safe_args else 0
    script = f"""
$ErrorActionPreference = 'Stop'
$root = '{RDP_ALLOWLIST}'
$apps = Join-Path $root 'Applications'
if (-not (Test-Path $apps)) {{ New-Item -Path $apps -Force | Out-Null }}
$key = Join-Path $apps '{safe_key}'
if (-not (Test-Path $key)) {{ New-Item -Path $key -Force | Out-Null }}
Set-ItemProperty -Path $root -Name fDisabledAllowList -Value 0 -Type DWord
Set-ItemProperty -Path $key -Name Name -Value '{safe_name}' -Type String
Set-ItemProperty -Path $key -Name Path -Value '{safe_path}' -Type String
Set-ItemProperty -Path $key -Name VPath -Value '{safe_path}' -Type String
Set-ItemProperty -Path $key -Name IconPath -Value '{safe_path}' -Type String
Set-ItemProperty -Path $key -Name IconIndex -Value 0 -Type DWord
Set-ItemProperty -Path $key -Name CommandLineSetting -Value {command_value} -Type DWord
Set-ItemProperty -Path $key -Name RequiredCommandLine -Value '{safe_args}' -Type String
Set-ItemProperty -Path $key -Name ShowInTSWA -Value 0 -Type DWord
Get-ItemProperty -Path $key | Select-Object Name,Path,CommandLineSetting,RequiredCommandLine | ConvertTo-Json -Compress
"""
    result = transport.run_powershell(script, timeout=45)
    if not result.ok:
        raise VemuError(f"Cannot add RemoteApp '{name}': {result.output}")
    return _json(result)


def remove_remoteapp_app(
    transport: GuestTransport,
    name: str,
) -> dict[str, Any]:
    """Remove one application from the machine-wide RemoteApp allowlist."""
    if not name.strip():
        raise VemuError("RemoteApp name is required.")
    safe_name = _escape(name.strip())
    safe_key = _escape(name.strip().replace("\\", "-"))
    script = f"""
$ErrorActionPreference = 'Stop'
$apps = Join-Path '{RDP_ALLOWLIST}' 'Applications'
$removed = $false
$key = Join-Path $apps '{safe_key}'
if (Test-Path -LiteralPath $key) {{
  Remove-Item -LiteralPath $key -Recurse -Force
  $removed = $true
}}
Get-ChildItem -LiteralPath $apps -ErrorAction SilentlyContinue | ForEach-Object {{
  $value = Get-ItemProperty -LiteralPath $_.PSPath -ErrorAction SilentlyContinue
  if ($value.Name -eq '{safe_name}') {{
    Remove-Item -LiteralPath $_.PSPath -Recurse -Force
    $removed = $true
  }}
}}
$removed | ConvertTo-Json -Compress
"""
    result = transport.run_powershell(script, timeout=45)
    if not result.ok:
        raise VemuError(f"Cannot remove RemoteApp '{name}': {result.output}")
    data = _json(result)
    return {"name": name.strip(), "removed": bool(data)}


def remoteapp_state(transport: GuestTransport) -> dict[str, Any]:
    return _query_rdp(transport)


def discover_apps(transport: GuestTransport) -> list[dict[str, str]]:
    """Return Start-menu apps visible to the transport's user."""
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress
"""
    result = transport.run_powershell(script, timeout=45)
    if not result.ok:
        raise VemuError(f"Cannot discover Start apps: {result.output}")
    data = _json(result)
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    return [
        {
            "name": str(item.get("Name", "")),
            "app_id": str(item.get("AppID", "")),
        }
        for item in data
        if isinstance(item, dict)
    ]
