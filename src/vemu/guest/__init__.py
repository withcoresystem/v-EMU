"""Shared Windows guest control used by seamless mode and future 3D work."""

from .optimize import (
    OptimizationReport,
    apply_profile as apply_optimization_profile,
)
from .qga import QGATransport
from .ssh import SSHTransport, ensure_keypair, scan_host_key
from .transport import CommandResult, GuestTransport
from .windows import WindowsInfo, query_windows_info

__all__ = [
    "CommandResult",
    "GuestTransport",
    "OptimizationReport",
    "QGATransport",
    "SSHTransport",
    "WindowsInfo",
    "apply_optimization_profile",
    "ensure_keypair",
    "query_windows_info",
    "scan_host_key",
]
