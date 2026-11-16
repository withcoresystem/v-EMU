# v-EMU Windows guest optimization

Experimental guest-side helper for Windows lab VMs. It is **not** a v-EMU
service, not a supported feature and not required for v-EMU to work. The
settings are conservative and reversible by reverting the VM snapshot.

The script and its `profiles.json` are intentionally usable **outside v-EMU**:
copy `contrib/windows/` to a Windows machine, run it elevated, and it works
without the daemon, UI or any hidden service. It stays public and fully open;
the future Python `vemu.guest.optimize` module is planned to share the same
profile data, not to replace or hide this standalone tool.

The script is inspired by common VDI optimization practice, including the
Omnissa Horizon OS Optimization Tool. No Omnissa code or template is bundled;
the profiles were written from functional settings. Design notes and the
planned shared-module overlay remain in the private development tree.

## Requirements

- Windows 10/11 or Windows Server 2022/2025.
- PowerShell 5.1 or later, run **elevated**.
- A VM snapshot before the first run.
- Optional: QEMU guest agent, VirtioFS/WinFsp, OpenSSH and USB passthrough
  already installed if you use them. The script preserves them.

## Usage

```powershell
# Show the plan only
.\vemu-guest-optimize.ps1 -Profile Safe -DryRun

# Recommended first pass for a lab image
.\vemu-guest-optimize.ps1 -Profile Safe

# Add SysMain/WSearch and unused consumer services
.\vemu-guest-optimize.ps1 -Profile Balanced

# Add Game DVR/fullscreen-optimization tweaks (Safe is included)
.\vemu-guest-optimize.ps1 -Profile 3D

# Apply the current user settings to a specific profile hive as well
.\vemu-guest-optimize.ps1 -Profile Safe -UserHive C:\Users\lab\NTUSER.DAT
```

`Balanced` and `3D` include the shared `Safe` settings. To combine both, run
`-Profile Balanced` and then `-Profile 3D`.

The Default user hive is always updated so new users inherit the settings.
`HKCU` is updated when the script is not running as SYSTEM. When run through
QGA/System, pass `-UserHive` for an existing profile or log in as the user and
run the script there.

Reboot after a run to apply service, shell and power changes. The log is
written to `%TEMP%\vemu-guest-optimize.log` unless `-LogPath` is used.

## Profiles

| Profile | Adds |
| --- | --- |
| `Safe` | Telemetry/CEIP/error reporting/diagnostics, consumer content, news/widgets/Copilot, animations/transparency, Game DVR/Game Mode, power/3D responsiveness |
| `Balanced` | `SysMain`, `WSearch`, Maps/location/Xbox/Phone/RemoteRegistry, WiFi/WWAN/UPnP maintenance tasks |
| `3D` | Game DVR capture, Game Bar startup, fullscreen-optimization behavior |

Power changes are common to all profiles:

- High performance power plan;
- no automatic standby, hibernate, monitor or disk timeout;
- hibernation/Fast Startup off;
- USB selective suspend and PCIe ASPM off (USB passthrough stability).

## What is preserved

The script refuses to disable:

- QEMU guest agent (`QEMU-GA`) and virtio-serial;
- VirtioFS/Virtio FS (`WinFsp`, `viofs`, `VirtioFsSvc`);
- USB controllers/services and SPICE;
- balloon (`BalloonService`);
- `vgpusrv`/`VioGpu*` (vgpu3d);
- OpenSSH (`sshd`);
- core RPC, device installation, power, audio, printing, RDP, SMB, firewall,
  Defender, BitLocker and Windows Update services.

`-DryRun` prints the plan without changing the guest. The apply run takes a
before/after snapshot of the protected services and the display device and
fails if a previously running protected service stops.

## What is not included

- Defender real-time protection, firewall, Security Center, SmartScreen, HVCI;
- BitLocker/Device Encryption;
- Windows Update disabling/deferral;
- Edge Update disabling;
- mass AppX/Store removal;
- crash dump, System Restore or Reserved Storage removal.

Those belong to an explicit disposable/offline tier, not this helper.

## Rollback

There is no per-item rollback. Revert the VM snapshot, or run the matching
profile's settings manually. The script is idempotent, so re-running a profile
is safe.
