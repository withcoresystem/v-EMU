# Windows 3D (experimental, CLI only)

> **Status: experimental and unsupported.** 0.3.0 ships one hidden command
> group, `vemu experimental 3d`, behind the **Experimental features** toggle in
> Settings. There is no 3D page and no 3D wizard in the desktop app. The
> feature can be removed without affecting normal VMs.
>
> The helper automates the validated `vgpu3d` console profile. It does not make
> 3D a supported feature, does not add GPU passthrough, and does not provide 3D
> over RDP, RemoteApp or seamless mode.

This page is the operator guide. The exact XML record and internal design
notes are not part of the public documentation set.

## What it is

One reversible action on an existing Windows 11 VM:

```text
VNC/QXL console
  -> SPICE + egl-headless + virtio-vga-gl (Venus)
  -> pinned vgpu3d test-signed WDDM driver in the guest
```

The result is a console-only lab VM for light graphics work (Blender, a
CAD-like app, a WebGL benchmark). It is **not**:

- a new VM profile, GPU passthrough, VFIO, SR-IOV, mediated devices, NVIDIA
  vGPU, AMD MxGPU, Intel GVT-g or GPU-P;
- a Secure Boot-compatible driver;
- 3D over RDP, RemoteApp, seamless mode, Parsec or Sunshine/Moonlight;
- a gaming or production desktop;
- an upstream-supported feature of v-EMU.

3D and seamless are mutually exclusive in practice. The helper pauses an active
seamless state before conversion and leaves it paused after `revert`.

## Requirements

### Host (Fedora/RHEL-family is the tested Tier 1 path)

| Component | Requirement |
| --- | --- |
| QEMU | `virtio-vga-gl` with `blob`, `hostmem` and `venus` properties |
| libvirt | new enough for `egl-headless` and `qemu:commandline` |
| virglrenderer | built with Venus; `virgl_render_server` present |
| GPU | a host Vulkan driver that supports Venus and dma-buf export |
| Render node | `/dev/dri/renderD*` readable by the QEMU user (usually `qemu` in the `render` group) |
| Console | `remote-viewer` from the `virt-viewer` package |

The tested host is Fedora 45 with QEMU 11.1.3, libvirt 12.6.0, virglrenderer
1.3.0 and an AMD Renoir RADV GPU. NVIDIA is the upstream test target; Intel is
untested here.

The helper checks these and refuses to continue when the render node, Venus
properties or viewer are missing. If `remote-viewer` is missing and the operator
confirmed the action, the helper installs `virt-viewer` through `dnf`, `zypper`,
`apt-get` or `pacman`.

### Host-wide sandbox setting

`vgpu3d` needs QEMU's seccomp sandbox off:

```ini
# /etc/libvirt/qemu.conf
seccomp_sandbox = 0
```

This is **host-wide and affects every VM**. Without it, QEMU's `spawn=deny`
sandbox blocks `virgl_render_server`, the guest gets no Venus capset, and 3D
fails even though the driver looks installed.

The helper does **not** change `qemu.conf` silently:

1. it reads the current value and prints the warning;
2. it refuses unless `--accept-host-security-change` (or the CLI confirmation)
   was given;
3. it saves `/etc/libvirt/qemu.conf.vemu-3d-backup` before editing;
4. it refuses if another VM is running, because restarting libvirt would
   disrupt it;
5. `revert` restores the backup and restarts libvirt.

Run the helper through `vemud` (the default) so root-owned files and libvirt
restarts work. `--direct` is for development and cannot read `qemu.conf` or
write root-owned chain files.

### Guest

The operator must prepare the Windows guest before using the helper:

- Windows 11 Pro/Enterprise **x64** (tested build 26300/26H2);
- a local account with an existing profile (log in once), or an administrator
  account;
- **Secure Boot off** (or "capable but no keys enrolled"). The driver is
  test-signed; the helper cannot disable firmware Secure Boot for you. Keep the
  BitLocker/TPM recovery key ready;
- QEMU guest agent running;
- OpenSSH Server installed, running, and TCP 22 reachable from the host;
- no Parsec or other third-party virtual display driver. Parsec's VDD plus
  vgpu3d caused XAML/WinUI crashes in testing.

The helper does not install OpenSSH, enable the guest firewall or create the
Windows profile. It stops with an actionable error if a prerequisite is
missing.

The helper enables test signing (`bcdedit /set testsigning on`) and turns
hibernation/fast startup off (`powercfg /h off`) before installing the driver.
This is expected to show the Windows "Test Mode" watermark.

A known upstream installer issue is handled automatically: when setup is
launched from OpenSSH/QGA, its `install.ps1` can fail at
`Import-Certificate` with `E_ACCESSDENIED`. The helper detects the failure,
patches the extracted `install.ps1` to use `certutil -addstore -f Root` and
`certutil -addstore -f TrustedPublisher`, and reruns it. If that fallback also
fails, the conversion rolls back and the error is reported.

### Driver package

The helper downloads, caches and verifies the pinned release; the guest does not
need internet access.

| Item | Value |
| --- | --- |
| Release | `v1.0.26313.18562` (2026-11-09) |
| Installer | `vgpu3d-setup-1.0.26313.18562.exe` |
| SHA-256 | `23f9b53d9b2c3bc4b5c6862b5b790a2c09f9e7d0f2063cddc71032476532ef94` |
| Manual package | `vgpu3d-m1.zip` |
| SHA-256 | `d60e0e3a24eabc209c870c6072305db78b35a18c949682d9433775bc11098dba` |
| Upstream | <https://github.com/fastchain/vgpu3d> |

The checkout must pin this release. Do not auto-update: upstream has had long
quiet periods, and a driver change can invalidate the XML, the test-signing
path or the 3D evidence. The installer exits `0` when installed and `3010` when
a reboot is required; the helper reboots and verifies the adapter after either
result.

The upstream repository has no top-level LICENSE file; new files carry SPDX MIT
and the KMD fork is BSD-3-Clause. Review the licences before redistributing
binaries. v-EMU does not patch Microsoft binaries.

## Enable the feature gate

The CLI is deliberately hidden behind a Settings toggle:

1. start `vemu ui`;
2. open **Settings**;
3. enable **Experimental features (unsupported)** and accept the warning.

The toggle writes `~/.config/vemu/experimental.json` and does not convert any
VM. Turning it off hides the CLI; it does **not** revert a converted VM.
Settings lists any existing experimental 3D state and warns before disabling,
including whether a host `qemu.conf` change was recorded. `status`, `revert`,
`repair` and `restore-host` remain available with the toggle off so a converted
VM and the host can always be recovered.

For headless automation the same file can be written directly:

```bash
mkdir -p ~/.config/vemu
printf '{"enabled": true}\n' > ~/.config/vemu/experimental.json
```

## Commands

```bash
# Read-only: state, checkpoint, console, host checks, guest adapter.
vemu experimental 3d status WIN-VM

# Show the planned XML and host changes without touching anything.
vemu experimental 3d enable WIN-VM --username WinUser --dry-run

# Convert one VM. The default is interactive.
vemu experimental 3d enable WIN-VM --username WinUser

# Automation: no prompt; also allows the host-wide sandbox change.
vemu experimental 3d enable WIN-VM --username WinUser --yes \
  --accept-host-security-change

# Keep a failed conversion for debugging instead of auto-reverting.
vemu experimental 3d enable WIN-VM --username WinUser --keep-broken

# Restore the disk checkpoint, the original XML and qemu.conf backup.
vemu experimental 3d revert WIN-VM

# Recover a latched VgpuBootGuard (Code 37) without reverting.
vemu experimental 3d repair WIN-VM

# Restore qemu.conf after all enabled 3D VMs are gone (host-wide).
vemu experimental 3d restore-host
```

`status` and `revert` work even when the experimental toggle is off, so a VM can
always be rolled back. `repair` also works with the toggle off; the VM must be
running.

### What `enable` does

1. Read-only host and guest prechecks; verify the pinned installer hash.
2. If seamless mode is active, pause it (keep the guest setup and checkpoint;
   remove the published shortcuts/icons and stop the supervisor). It does not
   run a seamless rollback.
3. Shut the VM down.
4. Save the current XML to `/var/lib/vemu/experimental3d/<domain>.domain.xml`
   and create a v-EMU snapshot labelled `experimental-3d`.
5. Optionally set `seccomp_sandbox = 0` (with consent and a backup), then
   restart libvirt.
6. Redefine the VM with the validated XML: memfd/shared memory, SPICE,
   `egl-headless`, `virtio-vga-gl` and the three `-global` properties.
7. Start the VM, wait for QGA, install a dedicated SSH key through QGA, enable
   test signing and fast-startup suppression, reboot.
8. Upload and run the pinned installer silently, reboot if it returns `3010`,
   and verify that Windows exposes `vgpu VirtIO GPU 3D (WDDM)` with the pinned
   driver version.
9. Write `status: enabled` to the state file.

State file: `/var/lib/vemu/experimental3d/<domain>.json` (mode `0640`, group
`vemu`). It contains the snapshot layer id, XML hash, installer hash, host
changes, paused seamless flag and verification output. It never contains a
Windows password.

If any step fails after the VM was modified, the helper automatically rolls
back unless `--keep-broken` was given. The error is reported after the rollback.

### What `revert` does

1. Stop the VM if needed.
2. Revert the v-EMU chain to the `experimental-3d` layer.
3. Restore the saved XML and the original console/video model.
4. Restore `/etc/libvirt/qemu.conf` and restart libvirt if the helper changed it.
5. Delete the state file.

The VM is left **shut down**. Seamless mode is **not** reactivated
automatically; use `vemu seamless prepare` (or the desktop app) when ready.
The guest driver and test signing are left installed; remove them from Windows
if the VM will be used without 3D. `virt-viewer` is a shared host package and is
not removed.

If the snapshot was compacted or deleted, `revert` reports that automatic disk
rollback is unavailable. Restore the XML backup manually before starting the VM.

### Deleting a converted VM

Deleting a VM does not require `revert` first; v-EMU removes the host 3D state
according to the delete mode:

- **Delete VM only** stops publishing, keeps the state/XML/SSH files for
  recovery and keeps the chain/checkpoint.
- **Delete VM and disks** purges the 3D state, XML and SSH files too.
- **Host security state is not restored automatically.** If the helper changed
  `/etc/libvirt/qemu.conf` (`seccomp_sandbox = 0`), the delete result reports
  the backup path and leaves the host setting in place. This is intentional for
  the 0.3.0 experiment.

After all enabled 3D VMs are reverted/deleted and every VM is shut down, use
**Doctor → Restore host security…** or:

```bash
vemu experimental 3d restore-host
```

Doctor shows the current `seccomp_sandbox` value, remaining 3D state and the
backup path. The restore action uses the saved backup; if no backup exists it
sets `seccomp_sandbox = 1` (the secure default). It is refused while a 3D VM is
still enabled or another VM is running, because it restarts libvirt.

If host security matters, run `vemu experimental 3d revert DOMAIN` before
deleting the VM, while it still exists and can be shut down.

### What the state looks like

```bash
vemu experimental 3d status WIN-VM
```

```text
Experimental 3D: WIN-VM
  enabled:    yes
  status:     enabled
  release:    v1.0.26313.18562
  checkpoint: present
  console:    spice, egl-headless
  video:      virtio
  adapter:    vgpu VirtIO GPU 3D (WDDM) (1.0.26313.18562)
  render:     /dev/dri/renderD128
  seccomp:    0
  viewer:     /usr/bin/remote-viewer
```

## Verification

The helper verifies what can be checked without an interactive desktop:

- the vgpu3d adapter exists, reports the pinned driver version, and Windows
  reports it as **OK** with problem code `0`;
- the VM uses SPICE + `egl-headless` with `virtio-vga-gl`;
- the checkpoint and XML backup exist;
- QGA and SSH survive the reboots.

Rapid reboots during installation can latch the driver's boot guard and leave
the adapter at **Code 37** (`CM_PROB_FAILED_DRIVER_ENTRY`) even though the
version string is correct. The helper detects that state and resets
`HKLM\SYSTEM\CurrentControlSet\Services\VioGpu3D\VgpuBootGuard` to `0`,
restarts the display device, and verifies again. If Code 37 appears after a
later reboot, apply the same recovery manually: set the value to `0`, then run
`pnputil /restart-device "<display instance id>"` (or reboot).

GPU-heavy checks must run in the **interactive console session**, not in session
0. QGA/SSH commands run as `SYSTEM` in session 0 and can report false
`VK_ERROR_DEVICE_LOST` errors. In the SPICE desktop:

- Device Manager: `vgpu VirtIO GPU 3D (WDDM)`, status OK, no problem code;
- `vgpuvram` returns a non-zero host-memory value;
- `dxdiag` reports DDI 12 and feature levels;
- a light 3D app starts and renders;
- `remote-viewer` displays the desktop and accepts input.

## Known limitations

| Area | Limit |
| --- | --- |
| Console | SPICE/`remote-viewer` only; VNC and the embedded noVNC console cannot display `virtio-vga-gl`. The mini console uses `virsh screenshot`, which returns `no surface` for this device; it may show a stale frame or "No signal". Use `vemu console --open`. |
| Driver boot guard | Rapid reboots can latch `VgpuBootGuard` and show Code 37 with a black screen. The helper resets it and restarts the device during `enable`; if it appears later, set it to `0` and restart the display device (or reboot). |
| RemoteApp/RDP | RDP uses the Microsoft Remote Display Adapter; there is no 3D in the session. Do not combine with seamless. |
| Secure Boot | Must be off/not enforced. Test signing is required; BitLocker/TPM may ask for recovery. |
| Host security | `seccomp_sandbox = 0` weakens QEMU for every VM. |
| Driver maturity | Test-signed, single-vendor, short release history. Windows updates, sleep/resume and driver updates can break it. |
| Power management | S3/S4 were unreliable in testing. Use full shutdowns; fast startup is disabled by the helper. |
| XAML/Parsec | Remove Parsec and other virtual display drivers; the combination crashed XAML/WinUI apps. |
| 32-bit OpenGL | One 32-bit `libgallium_wgl32.dll` crash was observed. |
| Streaming | Sunshine/Moonlight capture was black; Parsec was not usable. There is no hardware encoder. |
| Multi-GPU hosts | Pick the render node deliberately; the helper uses the first `/dev/dri/renderD*`. |
| Fork support | Upstream is a small experimental project. Pin the release and keep the XML/evidence in this repository. |

## Troubleshooting a black screen

Check the adapter state first, from the guest:

```powershell
Get-CimInstance Win32_VideoController |
  Select-Object Name,DriverVersion,Status,ConfigManagerErrorCode
```

If the vgpu adapter is **Code 37** (`CM_PROB_FAILED_DRIVER_ENTRY`), the display
can be black even though the driver version looks correct. This is the known
`VgpuBootGuard` latch after rapid reboots, not a missing driver.

The simplest recovery is the helper, with the VM running:

```bash
vemu experimental 3d repair WIN-VM
```

It resets the guard and restarts the display device without touching the disk
checkpoint. The manual equivalent is:

```powershell
$svc = 'HKLM:\SYSTEM\CurrentControlSet\Services\VioGpu3D'
Set-ItemProperty -Path $svc -Name VgpuBootGuard -Value 0 -Type DWord
$dev = Get-PnpDevice -Class Display -PresentOnly |
  Where-Object { $_.InstanceId -like 'PCI\VEN_1AF4&DEV_1050*' } |
  Select-Object -First 1
pnputil /restart-device "$($dev.InstanceId)"
```

Then close and reopen the SPICE console. A reboot with the guard at `0` also
works. The helper performs this recovery automatically during `enable` when it
sees Code 37.

The embedded mini console is not a SPICE/3D viewer: it uses `virsh screenshot`,
which returns `no surface` for `virtio-vga-gl`, so it may show a stale frame or
"No signal". Use the external SPICE console for the real display.

## Security and support stance

- v-EMU does not distribute, apply or support binary patches to Microsoft
  components.
- The driver is third-party, test-signed software installed inside the guest.
- The helper changes host security only after explicit consent and keeps a
  backup; it refuses to touch a host with other running VMs.
- The feature is best-effort. There is no SLA, no Secure Boot support and no
  promise that upstream will maintain the driver.
- Test on a dedicated VM. Do not use it on a production Windows desktop or with
  data you cannot lose.

Report issues with: v-EMU version, host distribution, QEMU/libvirt/virglrenderer
versions, GPU and Vulkan driver, Windows build, driver release, Secure Boot/test
signing state, and the v-EMU/SPICE/`/var/log/libvirt/qemu/<vm>.log` logs.

## FAQ

**Can I keep Secure Boot on?**
No. The driver is test-signed. A certificate in the Windows store is not a
Microsoft kernel signature.

**Can I use 3D with RemoteApp or seamless mode?**
No. RDP/RAIL sessions use the Microsoft Remote Display Adapter. Seamless and 3D
are separate modes; the helper pauses seamless before conversion.

**Can I use the normal VNC console?**
No. `virtio-vga-gl` needs a GL context. SPICE + `remote-viewer` is the tested
path.

**Can I use a VM that is already seamless?**
Yes, but the helper pauses seamless and removes its published shortcuts first.
Prefer a dedicated 3D VM. After `revert`, activate seamless again manually.

**Does `revert` uninstall the Windows driver?**
No. It restores the VM/disk/XML and the host setting. Uninstall `vgpu3d` from
Windows Settings and turn test signing off if you no longer need 3D.

**Where is the code for a fork?**
`src/vemu/experimental3d.py`, the `experimental3d.*` daemon methods, the
`experimental` CLI group and `tests/test_experimental3d.py`. The design record
contains the exact XML and the evidence needed to re-validate a fork.
