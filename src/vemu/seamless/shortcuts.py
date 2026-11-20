"""Linux desktop shortcuts for published seamless programs.

A shortcut only calls ``vemu seamless launch-app``; it never starts FreeRDP
itself, so all programs reuse one supervisor connection.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..errors import VemuError

_SLUG = re.compile(r"[^A-Za-z0-9._+-]+")


def applications_dir() -> Path:
    configured = os.environ.get("XDG_DATA_HOME")
    if configured:
        return Path(configured) / "applications"
    return Path.home() / ".local" / "share" / "applications"


def _slug(value: str) -> str:
    text = _SLUG.sub("-", value.strip()).strip("-")
    return text or "app"


def shortcut_filename(domain: str, name: str) -> str:
    return f"vemu-seamless-{_slug(domain)}-{_slug(name)}.desktop"


def shortcut_path(
    domain: str,
    name: str,
    directory: str | Path | None = None,
) -> Path:
    base = Path(directory).expanduser() if directory else applications_dir()
    return base / shortcut_filename(domain, name)


def _quote(value: str) -> str:
    if not value:
        return '""'
    if not any(character in value for character in ' \t"\'\\`$'):
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("`", "\\`")
        .replace("$", "\\$")
    )
    return f'"{escaped}"'


def desktop_family(value: str | None = None) -> str:
    """Return the small set of desktop families v-EMU supports."""
    text = (
        value
        or os.environ.get("XDG_CURRENT_DESKTOP")
        or os.environ.get("DESKTOP_SESSION")
        or ""
    ).casefold()
    if "gnome" in text:
        return "gnome"
    if "kde" in text or "plasma" in text:
        return "kde"
    return ""


def startup_wm_class(value: str | None = None) -> str:
    """Return StartupWMClass for GNOME; KDE keeps per-window icons."""
    return "RAIL" if desktop_family(value) == "gnome" else ""


def render(
    domain: str,
    app: dict[str, Any],
    *,
    icon: str = "",
    desktop: str | None = None,
) -> str:
    name = str(app.get("name", "")).strip()
    command = shutil.which("vemu") or "vemu"
    icon_line = (
        f"Icon={icon}"
        if icon and Path(icon).is_file()
        else "Icon=applications-windows"
    )
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        f"Name={name} ({domain})",
        "Comment=Windows program via v-EMU seamless mode",
        "Exec="
        + " ".join(
            [
                _quote(command),
                "seamless",
                "launch-app",
                _quote(domain),
                _quote(name),
            ]
        ),
        icon_line,
        "Terminal=false",
        "Categories=Utility;",
        f"X-vEMU-Seamless-Domain={domain}",
        f"X-vEMU-Seamless-App={name}",
    ]
    wm_class = startup_wm_class(desktop)
    if wm_class:
        lines.append(f"StartupWMClass={wm_class}")
    lines.append("")
    return "\n".join(lines)


def _adopt(path: Path, directory: Path) -> None:
    if os.geteuid() != 0:
        return
    try:
        info = directory.stat()
        os.chown(path, info.st_uid, info.st_gid)
    except OSError:
        pass


def _refresh_database(directory: Path) -> None:
    commands = [
        [shutil.which("update-desktop-database") or "", str(directory)],
        [shutil.which("kbuildsycoca6") or "", "--noincremental"],
        [shutil.which("kbuildsycoca5") or "", "--noincremental"],
    ]
    for command in commands:
        if not command[0]:
            continue
        try:
            subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue


def _ensure_directory(path: Path) -> None:
    """Create the shortcut directory and keep it owned by the desktop user."""
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


def install(
    domain: str,
    app: dict[str, Any],
    *,
    directory: str | Path | None = None,
    desktop: str | None = None,
) -> Path:
    """Create or replace one user-level .desktop entry."""
    name = str(app.get("name", "")).strip()
    path = str(app.get("path", "")).strip()
    if not name or not path:
        raise VemuError("Shortcut name and program path are required.")
    icon = str(app.get("icon") or "").strip()
    target = shortcut_path(domain, name, directory)
    _ensure_directory(target.parent)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        render(domain, app, icon=icon, desktop=desktop), encoding="utf-8"
    )
    os.chmod(temporary, 0o755)
    _adopt(temporary, target.parent)
    temporary.replace(target)
    _refresh_database(target.parent)
    return target


_RAIL_WM_CLASS = re.compile(
    r"^StartupWMClass=RAIL[ \t]*(?:\r?\n|$)", re.MULTILINE | re.IGNORECASE
)


def repair(
    *,
    directory: str | Path | None = None,
    desktop: str | None = None,
) -> list[Path]:
    """Remove stale shared RAIL desktop associations on non-GNOME desktops.

    All RAIL windows from one FreeRDP connection share ``WM_CLASS=RAIL``.
    Writing ``StartupWMClass=RAIL`` into every shortcut makes KDE associate
    an arbitrary app's launcher (and its icon) with any RAIL window. A
    shortcut generated while GNOME was active can therefore keep breaking
    KDE after a desktop switch, including shortcuts from another domain.
    """
    if desktop_family(desktop) == "gnome":
        return []
    base = Path(directory).expanduser() if directory else applications_dir()
    changed: list[Path] = []
    for path in sorted(base.glob("vemu-seamless-*.desktop")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if "X-vEMU-Seamless-Domain=" not in text:
            continue
        updated = _RAIL_WM_CLASS.sub("", text)
        if updated == text:
            continue
        if updated and not updated.endswith("\n"):
            updated += "\n"
        temporary = path.with_suffix(path.suffix + ".tmp")
        try:
            temporary.write_text(updated, encoding="utf-8")
            os.chmod(temporary, 0o755)
            _adopt(temporary, path.parent)
            temporary.replace(path)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            continue
        changed.append(path)
    if changed:
        _refresh_database(base)
    return changed


def remove(
    domain: str,
    name: str,
    *,
    directory: str | Path | None = None,
) -> bool:
    target = shortcut_path(domain, name, directory)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    _refresh_database(target.parent)
    return True


def remove_all(
    domain: str,
    *,
    directory: str | Path | None = None,
) -> list[Path]:
    base = Path(directory).expanduser() if directory else applications_dir()
    prefix = f"vemu-seamless-{_slug(domain)}-"
    removed: list[Path] = []
    for item in base.glob(prefix + "*.desktop"):
        try:
            item.unlink()
        except OSError:
            continue
        removed.append(item)
    if removed:
        _refresh_database(base)
    return removed

