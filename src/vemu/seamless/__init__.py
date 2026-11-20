"""Windows seamless mode (RDP/RemoteApp)."""

from .apps import (
    AppEntry,
    discover_start_menu_apps,
    parse_entries,
    with_compat_arguments,
)
from .launch import RailTest, test_rail_app
from .overlay import apply_overlay
from .prepare import SeamlessPrepare, prepare_seamless, state_dir
from .rdp import (
    add_remoteapp_app,
    discover_apps,
    enable_rdp,
    install_admin_key,
    preflight,
    remoteapp_state,
    remove_remoteapp_app,
)

__all__ = [
    "AppEntry",
    "RailTest",
    "SeamlessPrepare",
    "add_remoteapp_app",
    "apply_overlay",
    "discover_apps",
    "discover_start_menu_apps",
    "enable_rdp",
    "install_admin_key",
    "parse_entries",
    "prepare_seamless",
    "preflight",
    "remoteapp_state",
    "remove_remoteapp_app",
    "state_dir",
    "test_rail_app",
    "with_compat_arguments",
]
