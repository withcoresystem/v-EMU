"""Extract Windows executable icons for Linux .desktop shortcuts."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from ..errors import VemuError
from ..guest.transport import GuestTransport
from .apps import store_aumid

_SLUG = re.compile(r"[^A-Za-z0-9._+-]+")


def icon_dir() -> Path:
    configured = os.environ.get("XDG_DATA_HOME")
    base = Path(configured) if configured else Path.home() / ".local" / "share"
    return base / "vemu" / "seamless" / "icons"


def _slug(value: str) -> str:
    return _SLUG.sub("-", value.strip()).strip("-") or "app"


def icon_path(
    domain: str,
    name: str,
    directory: str | Path | None = None,
) -> Path:
    base = Path(directory).expanduser() if directory else icon_dir()
    return base / f"vemu-seamless-{_slug(domain)}-{_slug(name)}.png"


def _adopt(path: Path, directory: Path) -> None:
    if os.geteuid() != 0:
        return
    try:
        info = directory.stat()
        os.chown(path, info.st_uid, info.st_gid)
    except OSError:
        pass


def _ensure_dir(path: Path) -> None:
    if os.geteuid() != 0:
        path.mkdir(parents=True, exist_ok=True)
        return
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    owner = probe.stat() if probe.exists() else None
    path.mkdir(parents=True, exist_ok=True)
    if owner is None:
        return
    current = path
    while current != probe and current != current.parent:
        try:
            os.chown(current, owner.st_uid, owner.st_gid)
        except OSError:
            break
        current = current.parent


def _escape(value: str) -> str:
    return value.replace("'", "''")


def _write_png(
    payload: bytes, target: Path
) -> Path | None:
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    _ensure_dir(target.parent)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(payload)
    os.chmod(temporary, 0o644)
    _adopt(temporary, target.parent)
    temporary.replace(target)
    return target


def extract(
    transport: GuestTransport,
    program: str,
    domain: str,
    name: str,
    *,
    directory: str | Path | None = None,
) -> Path | None:
    """Copy the executable's associated icon to the host icon cache."""
    path = program.strip()
    if not path.lower().endswith(".exe"):
        return None
    digest = hashlib.sha1(
        f"{domain}|{name}|{path}".encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:12]
    remote = rf"C:\Windows\Temp\vemu-icon-{digest}.png"
    script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$source = '{_escape(path)}'
$target = '{_escape(remote)}'
if (-not (Test-Path -LiteralPath $source)) {{ throw 'program not found' }}
$icon = [System.Drawing.Icon]::ExtractAssociatedIcon($source)
if ($null -eq $icon) {{ throw 'no associated icon' }}
try {{
  $bitmap = $icon.ToBitmap()
  $bitmap.Save($target, [System.Drawing.Imaging.ImageFormat]::Png)
  $bitmap.Dispose()
}} finally {{
  $icon.Dispose()
}}
"""
    try:
        result = transport.run_powershell(script, timeout=45)
        if not result.ok:
            return None
        payload = transport.download(
            remote.replace("\\", "/"), max_bytes=2 * 1024 * 1024
        )
    except VemuError:
        return None
    finally:
        try:
            transport.run_powershell(
                f"Remove-Item -LiteralPath '{_escape(remote)}' "
                "-Force -ErrorAction SilentlyContinue",
                timeout=20,
            )
        except VemuError:
            pass
    target = icon_path(domain, name, directory)
    return _write_png(payload, target)


def extract_store(
    transport: GuestTransport,
    arguments: str,
    domain: str,
    name: str,
    *,
    directory: str | Path | None = None,
) -> Path | None:
    """Extract a packaged Store/MSIX app logo through explorer shell args."""
    aumid = store_aumid(arguments)
    if "!" not in aumid:
        return None
    package_family, app_id = aumid.split("!", 1)
    digest = hashlib.sha1(
        f"{domain}|{name}|{aumid}".encode("utf-8"), usedforsecurity=False
    ).hexdigest()[:12]
    remote = rf"C:\Windows\Temp\vemu-icon-{digest}.png"
    script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing
$family = '{_escape(package_family)}'
$appId = '{_escape(app_id)}'
$target = '{_escape(remote)}'
$package = Get-AppxPackage -ErrorAction SilentlyContinue |
  Where-Object {{ $_.PackageFamilyName -eq $family }} | Select-Object -First 1
if ($null -eq $package) {{ throw 'package not found' }}
$manifest = Get-AppxPackageManifest -Package $package.PackageFullName
$app = $manifest.Package.Applications.Application |
  Where-Object {{ $_.Id -eq $appId }} | Select-Object -First 1
if ($null -eq $app) {{ throw 'application not found' }}
$logo = [string]$app.VisualElements.Square44x44Logo
if (-not $logo) {{ $logo = [string]$app.VisualElements.Square150x150Logo }}
if (-not $logo) {{ throw 'no logo in manifest' }}
$source = Join-Path $package.InstallLocation $logo
if (-not (Test-Path -LiteralPath $source)) {{
  $dir = Split-Path -Parent $source
  $stem = [IO.Path]::GetFileNameWithoutExtension($source)
  $ext = [IO.Path]::GetExtension($source)
  $candidate = Get-ChildItem -LiteralPath $dir -File -Filter "$stem*$ext" |
    Sort-Object Length -Descending | Select-Object -First 1
  if ($null -ne $candidate) {{ $source = $candidate.FullName }}
}}
if (-not (Test-Path -LiteralPath $source)) {{ throw 'logo file not found' }}
$image = [System.Drawing.Image]::FromFile($source)
try {{
  $bitmap = New-Object System.Drawing.Bitmap 32, 32
  try {{
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {{
      $graphics.InterpolationMode =
        [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
      $graphics.DrawImage($image, 0, 0, 32, 32)
    }} finally {{ $graphics.Dispose() }}
    $bitmap.Save($target, [System.Drawing.Imaging.ImageFormat]::Png)
  }} finally {{ $bitmap.Dispose() }}
}} finally {{ $image.Dispose() }}
"""
    try:
        result = transport.run_powershell(script, timeout=45)
        if not result.ok:
            return None
        payload = transport.download(
            remote.replace("\\", "/"), max_bytes=2 * 1024 * 1024
        )
    except VemuError:
        return None
    finally:
        try:
            transport.run_powershell(
                f"Remove-Item -LiteralPath '{_escape(remote)}' "
                "-Force -ErrorAction SilentlyContinue",
                timeout=20,
            )
        except VemuError:
            pass
    return _write_png(payload, icon_path(domain, name, directory))


def remove(
    domain: str, name: str, *, directory: str | Path | None = None
) -> bool:
    try:
        icon_path(domain, name, directory).unlink()
    except OSError:
        return False
    return True


def remove_all(
    domain: str, *, directory: str | Path | None = None
) -> list[Path]:
    """Remove every cached icon generated for one seamless domain."""
    base = Path(directory).expanduser() if directory else icon_dir()
    prefix = f"vemu-seamless-{_slug(domain)}-"
    removed: list[Path] = []
    for item in base.glob(prefix + "*.png"):
        try:
            item.unlink()
        except OSError:
            continue
        removed.append(item)
    return removed
