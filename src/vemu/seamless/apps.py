"""Start-menu discovery and per-app compatibility flags for seamless mode."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable

from ..errors import VemuError
from ..guest.transport import CommandResult, GuestTransport

CHROMIUM_EXES = {
    "brave.exe",
    "chrome.exe",
    "chromium.exe",
    "code.exe",
    "discord.exe",
    "electron.exe",
    "msedge.exe",
    "msedgewebview2.exe",
    "notion.exe",
    "obsidian.exe",
    "opera.exe",
    "slack.exe",
    "spotify.exe",
    "teams.exe",
    "vivaldi.exe",
    "whatsapp.exe",
}
GPU_FLAGS = ("--disable-gpu", "--disable-gpu-compositing")
_MAX_APPS = 500
_SYSTEM_PREFIXES = ("c:\\windows",)
_NOISY_MARKERS = (
    "\\windows defender",
    "\\edgeupdate",
    "\\internet explorer\\",
    "\\windows mail\\",
    "\\windows photo viewer\\",
    "\\common files\\",
    "\\windowsapps\\",
    "\\qemu\\",
    "\\red hat\\",
    "\\winfsp\\",
    "\\virtio",
    "\\spice\\",
    "\\microsoft update health tools\\",
)
_NOISY_NAMES = {
    "copilot update",
    "microsoft edge update",
    "qemu",
    "red hat spice",
    "red hat virtio ethernet adapter",
    "sense forensics",
    "windows cyber defense",
    "winfsp",
}
_NOISY_STORE_NAMES = {
    "click to do",
    "copilot",
    "cross device",
    "get started",
    "windows backup",
    "windows security",
}
_STORE_EXCLUDED_PREFIXES = ("microsoftwindows.client.",)


def is_windows_system_app(path: str) -> bool:
    """Return True for built-in Windows-directory tools."""
    value = path.replace("/", "\\").strip().lower()
    return any(
        value == prefix or value.startswith(prefix + "\\")
        for prefix in _SYSTEM_PREFIXES
    )


def is_noisy_app(path: str, name: str = "") -> bool:
    """Reject updaters, Defender helpers and driver utilities."""
    value = path.replace("/", "\\").strip().lower()
    if any(marker in value for marker in _NOISY_MARKERS):
        return True
    return name.strip().casefold() in _NOISY_NAMES


@dataclass(frozen=True)
class AppEntry:
    """One Start-menu program that can be published as a RAIL shortcut."""

    name: str
    path: str
    arguments: str = ""
    shortcut: str = ""
    source: str = ""

    @property
    def executable(self) -> str:
        return self.path.replace("/", "\\").rsplit("\\", 1)[-1]

    @property
    def process_name(self) -> str:
        executable = self.executable
        return executable[:-4] if executable.lower().endswith(".exe") else executable

    @property
    def software_gpu(self) -> bool:
        return self.executable.lower() in CHROMIUM_EXES

    @property
    def launch_arguments(self) -> str:
        return with_compat_arguments(self.path, self.arguments)

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "path": self.path,
            "arguments": self.arguments,
            "shortcut": self.shortcut,
            "source": self.source,
        }


def _json(result: CommandResult) -> Any:
    try:
        return json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise VemuError(
            f"Start-menu query returned invalid JSON: {result.stdout[:300]}"
        ) from exc


def is_remoteapp_capable(path: str) -> bool:
    """Reject raw AUMIDs, shell aliases and non-executables."""
    value = path.strip()
    if not value or "!" in value or value.lower().startswith("shell:"):
        return False
    return value.lower().endswith(".exe")


def store_aumid(arguments: str) -> str:
    """Return the AUMID from an explorer shell:AppsFolder argument."""
    value = arguments.strip().strip('"')
    prefix = "shell:AppsFolder\\"
    if not value.lower().startswith(prefix.lower()):
        return ""
    aumid = value[len(prefix):].strip().strip('"')
    return aumid if "!" in aumid else ""


def with_compat_arguments(path: str, arguments: str | Iterable[str] = "") -> str:
    """Append software-GPU flags to known Chromium/Electron programs."""
    executable = path.replace("/", "\\").rsplit("\\", 1)[-1].lower()
    if isinstance(arguments, str):
        text = arguments.strip()
        present = text.split() if text else []
        additions = [
            flag for flag in GPU_FLAGS if flag not in present
        ] if executable in CHROMIUM_EXES else []
        return " ".join([text, *additions]).strip()
    values = [str(item) for item in arguments]
    if executable in CHROMIUM_EXES:
        for flag in GPU_FLAGS:
            if flag not in values:
                values.append(flag)
    return " ".join(values).strip()


def parse_entries(
    payload: Any,
    *,
    limit: int = _MAX_APPS,
    include_system: bool = False,
    include_store: bool = False,
) -> list[AppEntry]:
    """Normalize discovery output and drop unsupported/noisy entries."""
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []
    parsed: list[AppEntry] = []
    shortcut_paths: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        path = str(item.get("path") or "").strip()
        if not name or not is_remoteapp_capable(path):
            continue
        source = str(item.get("source") or "").strip()
        if source == "store":
            if not include_store:
                continue
            aumid = store_aumid(str(item.get("arguments") or ""))
            if not aumid:
                continue
            if aumid.casefold().startswith(_STORE_EXCLUDED_PREFIXES):
                continue
            if name.casefold() in _NOISY_STORE_NAMES:
                continue
        elif not include_system and is_windows_system_app(path):
            continue
        if is_noisy_app(path, name):
            continue
        entry = AppEntry(
            name=name,
            path=path,
            arguments=str(item.get("arguments") or "").strip(),
            shortcut=str(item.get("shortcut") or "").strip(),
            source=source,
        )
        if source == "start-menu":
            shortcut_paths.add(path.casefold())
        parsed.append(entry)
    entries: list[AppEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for entry in parsed:
        if (
            entry.source == "program-files"
            and entry.path.casefold() in shortcut_paths
        ):
            continue
        key = (
            entry.name.casefold(),
            entry.path.casefold(),
            entry.arguments.casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)
        if len(entries) >= limit:
            break
    entries.sort(key=lambda item: (item.name.casefold(), item.path.casefold()))
    return entries


def discover_start_menu_apps(
    transport: GuestTransport,
    *,
    include_system: bool = False,
    include_store: bool = False,
) -> list[AppEntry]:
    """List real installed programs for the transport's user.

    Start-menu shortcuts are preferred because they carry the vendor name and
    arguments. Program Files is scanned only one level deep as a fallback;
    built-in System32/SysWOW64 and Store/MSIX apps are hidden unless explicitly
    requested.
    """
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
$includeStore = __INCLUDE_STORE__
$shell = New-Object -ComObject WScript.Shell
$items = @()
$roots = @(
  "$env:ProgramData\Microsoft\Windows\Start Menu\Programs",
  "$env:APPDATA\Microsoft\Windows\Start Menu\Programs"
)
foreach ($root in $roots) {
  if (-not (Test-Path -LiteralPath $root)) { continue }
  Get-ChildItem -LiteralPath $root -Filter *.lnk -Recurse |
    ForEach-Object {
      $link = $shell.CreateShortcut($_.FullName)
      $target = [string]$link.TargetPath
      if (-not $target) { return }
      if ($target -notmatch '\.exe$') { return }
      if (-not (Test-Path -LiteralPath $target)) { return }
      $items += [pscustomobject]@{
        name = $_.BaseName
        path = $target
        arguments = [string]$link.Arguments
        shortcut = $_.FullName
        source = 'start-menu'
      }
    }
}
$skip = '^(unins|setup|update|updater|crash|crashpad|helper|service|.*svc.*|vcredist|dotnet|install|repair|report|register|uninst|.*proxy.*|.*notification.*|.*elevation.*|.*telemetry.*|sense.*|mpcmdrun|mpdefendercore.*|wab.*|ie.*|extExport|ielowutil|ieinstal|iediagcmd|qemu.*|spice.*|virtio.*|winfsp.*|balloon.*)$'
if ($includeStore) {
  Get-StartApps | Where-Object { $_.AppID -match '!' } | ForEach-Object {
    $appId = [string]$_.AppID
    if ($appId -match '^MicrosoftWindows\.Client\.') { return }
    $items += [pscustomobject]@{
      name = [string]$_.Name
      path = (Join-Path $env:WINDIR 'explorer.exe')
      arguments = "shell:AppsFolder\$appId"
      shortcut = ''
      source = 'store'
    }
  }
}
$skipDir = '^(windows defender.*|edgeupdate|internet explorer|windows mail|windows photo viewer|common files|windowsapps|modifiablewindowsapps|windows powershell|microsoft update health tools|red hat|qemu|winfsp|spice.*|virtio.*|installer|update|setup)$'
$shortcutPaths = @(
  $items | Where-Object { $_.source -eq 'start-menu' } |
    ForEach-Object { $_.path }
)
$programRoots = @($env:ProgramFiles, ${env:ProgramFiles(x86)})
foreach ($root in $programRoots) {
  if (-not $root) { continue }
  if (-not (Test-Path -LiteralPath $root)) { continue }
  Get-ChildItem -LiteralPath $root -Directory |
    Where-Object { $_.Name -notmatch $skipDir } |
    ForEach-Object {
      $appDir = $_
      $prefix = $appDir.FullName.TrimEnd('\') + '\'
      $covered = $false
      foreach ($shortcutPath in $shortcutPaths) {
        if ($shortcutPath.StartsWith(
          $prefix, [System.StringComparison]::OrdinalIgnoreCase
        )) {
          $covered = $true
          break
        }
      }
      if ($covered) { continue }
      $files = @(
        Get-ChildItem -LiteralPath $appDir.FullName -Filter *.exe -File
      )
      $files += @(
        Get-ChildItem -LiteralPath $appDir.FullName -Directory |
          Where-Object { $_.Name -notmatch $skipDir } |
          ForEach-Object {
            Get-ChildItem -LiteralPath $_.FullName -Filter *.exe -File
          }
      )
      foreach ($file in $files) {
        if ($file.Name -match $skip) { continue }
        $version = $file.VersionInfo
        if (-not $version.ProductName -and -not $version.FileDescription) {
          continue
        }
        if ($version.ProductName -match 'Microsoft.*Windows.*Operating System') {
          continue
        }
        if ($version.FileDescription -match '^Microsoft.*Windows.*') {
          continue
        }
        $name = if ($version.ProductName) {
          [string]$version.ProductName
        } else {
          [string]$appDir.Name
        }
        $items += [pscustomobject]@{
          name = $name
          path = $file.FullName
          arguments = ''
          shortcut = ''
          source = 'program-files'
        }
      }
    }
}
@($items) | Sort-Object name, path -Unique | ConvertTo-Json -Compress
"""
    script = script.replace(
        "__INCLUDE_STORE__", "$true" if include_store else "$false"
    )
    remote = r"C:\Windows\Temp\vemu-discover-apps.ps1"
    try:
        transport.upload(script.encode("utf-8"), remote)
    except VemuError as exc:
        raise VemuError(f"Cannot upload the app discovery script: {exc}") from exc
    try:
        result = transport.run_powershell(f"& '{remote}'", timeout=120)
    finally:
        try:
            transport.run_powershell(
                f"Remove-Item -LiteralPath '{remote}' -Force",
                timeout=30,
            )
        except VemuError:
            pass
    if not result.ok:
        raise VemuError(f"Cannot discover installed apps: {result.output}")
    return parse_entries(
        _json(result),
        include_system=include_system,
        include_store=include_store,
    )


def published_by_name(apps: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for app in apps:
        if not isinstance(app, dict):
            continue
        name = str(app.get("name") or "").strip()
        if name:
            result[name.casefold()] = app
    return result
