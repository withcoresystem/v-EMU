# Arch family — experimental host notes

Status: **experimental / as-is**. Arch Linux, CachyOS and Arch-based desktops
(including Omarchy) are **not** part of the official v-EMU host matrix. They
receive no feature-parity or response-time promise.

Image Builder remains disabled on these hosts: `image-builder` and osbuild are
not packaged for Arch, and the UI host gate rejects the family before tools are
checked.

## Manual installer

`contrib/setup-arch.sh` is a separate, experimental installer for core v-EMU
and optional seamless dependencies:

```bash
git clone https://github.com/withcoresystem/v-EMU
cd v-EMU
./contrib/setup-arch.sh --precheck
./contrib/setup-arch.sh
./contrib/setup-arch.sh --full   # aarch64/riscv64 TCG + firmware
```

It installs:

- `qemu-full`, `libvirt`, `libvirt-python`, `virt-install`, `dnsmasq`,
  `nftables`, `swtpm`, `edk2-ovmf`, `virtiofsd`, `pyside6`, `guestfs-tools`,
  `libisoburn`, `dosfstools`, `mtools`, `tigervnc`, `virt-viewer`, `freerdp`,
  `python`, `python-pip`, `python-keyring`, `polkit`;
- a venv with system PySide6/libvirt access and the `vemu`/`vemu-ui`/`vemud`
  links in `~/.local/bin`;
- the `vemu` group, libvirt/kvm group membership and desktop entry;
- the v-EMU firmware under `/usr/share/vemu/firmware`; `--full` also adds
  aarch64/riscv64 QEMU and the padded pflash/vars templates;
- the Windows guest optimization assets under `/usr/share/vemu/windows`
  (`vemu-guest-optimize.ps1`, `profiles.json`);
- shared host state under `/var/lib/vemu/seamless` and
  `/var/lib/vemu/experimental3d` (`root:vemu`, mode 0770);
- a daemon copy under `/usr/local/lib/vemu`, a `vemud.service` unit, polkit
  action and `/etc/vemu/qemu.env` with the Arch QEMU identity
  (`libvirt-qemu:libvirt-qemu`);
- modular libvirt units, the default NAT network and IPv4 forwarding.

The script is **not** included in the `.run` release artifact. It intentionally
does not modify `setup.sh` and does not add pacman support to the official
installer.

After installation, log out and back in so group membership applies, then:

```bash
vemu doctor
vemu ui
```

The first-run storage and network wizards remain the normal setup path.

## Validation on CachyOS

CachyOS rolling, kernel `7.3.4-1-cachyos`, QEMU 11.1.3, libvirt 12.9.0,
Python 3.14, PySide6 6.12:

| Area | Result |
|---|---|
| `/dev/kvm`, nested KVM | OK |
| `contrib/setup-arch.sh` fresh install | OK; packages, venv, service, firmware, groups |
| `vemud.service` and `vemu doctor` via daemon | OK; QEMU user `libvirt-qemu`, virtiofsd found |
| Storage roles without SELinux | OK; reports `Disabled` and uses `libvirt-qemu` ownership |
| VM create/define/start via daemon | OK; no manual ACL needed |
| VNC console, stats | OK |
| Snapshot create/list | OK through the daemon |
| Seal, clone | OK through the daemon |
| Network add/apply/status (NAT) | OK |
| USB nodedev | OK |
| `--full` aarch64/riscv64 QEMU and firmware | OK; doctor reports all three firmware sets |
| Windows seamless prepare/pause/remove | OK with FreeRDP 3.33.1 (`xfreerdp3`); Notepad RAIL test passed |
| Cloud image lifecycle | Debian 13 genericcloud: import, cloud-init clone, SSH, nested `/dev/kvm`, snapshot/revert, delete |
| Image Builder | Disabled, as expected |
| GUI | Offscreen start OK; KDE Wayland manual check |

### 0.3.0 live validation (2026-11-20)

The 0.3.0 run repeated `contrib/setup-arch.sh --full` on a clean CachyOS
guest, then exercised the two optional paths that matter for the last
Arch-based release.

- Host: CachyOS rolling, kernel `7.3.4-1-cachyos`, QEMU 11.1.3, libvirt
  12.9.0, Python 3.14, PySide6 6.12, FreeRDP 3.33.1.
- `pacman -Syu` was required first because the rolling repo had newer
  GStreamer packages than the installed set. A stale mirror/database also
  produced one 404; refreshing the databases and fully updating resolved both.
  A second `setup-arch.sh --full` run after install is idempotent.
- `setup.sh` is still not the Arch path: use `contrib/setup-arch.sh`.
- Cloud image: Debian 13 genericcloud was imported as a template, cloned with
  cloud-init and a static address, booted, reached SSH, exposed nested
  `/dev/kvm`, survived a reboot, snapshotted/reverted, and was deleted with its
  storage.
- Windows: a Windows 11 26H2 template was copied from a Fedora host, imported as a
  Windows template, cloned with OOBE, prepared for seamless mode through the
  daemon, and tested with a Notepad RAIL window. Pause removed shortcuts and
  kept the guest; remove restored the pre-seamless checkpoint and left the VM
  stopped.
- The installer now includes `freerdp`, `python-keyring`, the Windows guest
  optimization assets and the shared state directories. Without those, the
  initial Arch run failed at optimization-asset lookup and could not persist a
  seamless password.
- Headless RAIL testing used `xorg-server-xvfb`; it is a test tool, not an
  installer dependency. A desktop session provides the normal keyring backend;
  over plain SSH the keyring backend may be unavailable until the user logs
  into KDE.
- The VM keyring and Windows assets are host-user concerns; do not run the
  daemon with a user state directory. Use the shared `/var/lib/vemu` paths
  prepared by the installer.
- Cloud-init users now use `/bin/sh` instead of `/bin/bash`, so Alpine-style
  images can log in. The 0.3.0 cloud run used a static cloud-init address;
  DHCP from the test `vemu-NAT` profile was not confirmed and should be
  validated before relying on DHCP.
- A headless Edge RAIL test exited before the window settled; Notepad RAIL
  passed on the same VM. Treat that as an app-specific Edge test, not a
  converter failure.

`--full` installs `qemu-system-aarch64`, `qemu-system-riscv64`, the Arch
`edk2-aarch64`/`edk2-riscv64` packages and the padded v-EMU pflash/vars
templates. The stock Arch paths do not include the pflash names v-EMU checks,
but `vemu doctor` now reports the custom firmware instead:

```text
ok  v-EMU firmware  x86_64, aarch64, riscv64 (firmware 1.0)
ok  edk2-aarch64    v-EMU custom firmware: .../aarch64/QEMU_EFI-pflash.raw
ok  edk2-riscv64    v-EMU custom firmware: .../riscv64/RISCV_VIRT_CODE-pflash.raw
```

## Validation on Omarchy

Omarchy 4.5.0, kernel `7.2.15-1-omarchy`, QEMU 11.1.2, libvirt 12.9.0,
Python 3.14.8, PySide6 6.12. A clean precheck found no QEMU/libvirt/PySide6;
all required Arch packages were available in the repositories.

| Area | Result |
|---|---|
| `contrib/setup-arch.sh --precheck --full` before install | Correctly reported missing tools and firmware |
| `contrib/setup-arch.sh --full` | OK; packages, venv, daemon, groups, firmware |
| `vemud.service` and `vemu doctor` via daemon | OK; only the no-LSM warning |
| `--precheck --full` after install | OK |
| GUI under Hyprland Wayland | OK; v-EMU window and first-run setup opened |
| Image Builder | Disabled, as expected |

The Hyprland session required `XDG_RUNTIME_DIR`, `WAYLAND_DISPLAY` and
`QT_QPA_PLATFORM=wayland` when launching from SSH; a normal desktop launch uses
the session environment.

### aarch64 cloud image boot test

Tested with Alpine `generic_alpine-3.24.1-aarch64-uefi-cloudinit-r0.qcow2`
(229 MiB) and a NoCloud seed:

- custom `/usr/share/vemu/firmware/aarch64/QEMU_EFI-pflash.raw` + 64 MiB
  vars template;
- `qemu-system-aarch64` TCG, `virt` machine, virtio disk/network/GPU;
- boot reached the Alpine login prompt; cloud-init read the NoCloud seed;
- `vemu list` detects the domain as External; after the first start adds the
  VNC WebSocket, `vemu console` works after one stop/start.

This was a manual libvirt domain, not `vemu import`: disk import currently
builds an x86_64 domain, so aarch64 cloud images remain External/manual until
import learns the target architecture.

Known limitations:

- no SELinux/AppArmor label for image files; the lower isolation is accepted
  for this experimental tier;
- no Image Builder;
- no live VFIO test (validation host had no spare PCIe device);
- rolling-release package changes can break the script without a vEMU release;
- the daemon copy uses system Python and `PYTHONPATH=/usr/local/lib/vemu`; this
  is intentionally simple, not the packaged `.run` layout.

## Removing

```bash
./contrib/setup-arch.sh --uninstall
```

This removes the service, daemon copy and firmware. It keeps the venv, VMs,
storage and the `vemu` group.

Report issues with the CachyOS/Arch version, kernel, QEMU/libvirt versions and
`vemu doctor --json`. Do not treat an Arch issue as a release blocker until the
host tier becomes official.
