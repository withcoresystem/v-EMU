"""Seamless-mode guest overlay: disable browser hardware acceleration.

The common optimization profiles intentionally do not contain seamless-specific
settings.  This overlay is applied only when the user activates seamless mode.
Per-application Chromium/Electron flags live in :mod:`vemu.seamless.apps`.
"""

from __future__ import annotations

from typing import Any

from ..errors import VemuError
from ..guest.transport import GuestTransport

BROWSER_POLICIES = {
    r"HKLM:\SOFTWARE\Policies\Microsoft\Edge": "Microsoft Edge",
    r"HKLM:\SOFTWARE\Policies\Google\Chrome": "Google Chrome",
}


def apply_overlay(transport: GuestTransport) -> dict[str, Any]:
    """Disable hardware acceleration for Edge/Chrome in the guest."""
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$applied = @()",
    ]
    for key, label in BROWSER_POLICIES.items():
        lines.append(f"$key = '{key}'")
        lines.append(
            "if (-not (Test-Path $key)) { "
            "New-Item -Path $key -Force | Out-Null }"
        )
        lines.append(
            "Set-ItemProperty -Path $key -Name "
            "'HardwareAccelerationModeEnabled' -Value 0 -Type DWord"
        )
        lines.append(f"$applied += '{label}'")
    lines.append("$applied | ConvertTo-Json -Compress")
    result = transport.run_powershell("\n".join(lines), timeout=60)
    if not result.ok:
        raise VemuError(
            f"Cannot apply the seamless hardware-acceleration overlay: "
            f"{result.output}"
        )
    return {
        "hardware_acceleration_disabled": True,
        "browsers": sorted(BROWSER_POLICIES.values()),
    }
