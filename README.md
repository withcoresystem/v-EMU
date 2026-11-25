# v-EMU

**A local-first virtualization lab for Linux — templates, linked clones and
declarative labs on the KVM stack you already trust.**

![v-EMU main window](docs/images/main-window.webp)

v-EMU is a desktop application for repeatable, self-contained labs on one Linux
host. It drives QEMU/KVM and libvirt directly: no second hypervisor, no web
control plane, no cluster to babysit. Templates, storage, networks, consoles
and lab definitions live in one place, with a CLI for the same operations.

v-EMU is maintained by **Tran Trung Truc** at **CORESYSTEM VN** —
[coresystem.vn](https://coresystem.vn).

> **Status:** 0.4.0 host-support narrowing release. Fedora 45 and
> Alma/RHEL-family 10.3 are the only supported hosts; no new VM, guest,
> console, seamless or 3D features are added. Debian/Ubuntu, openSUSE/SUSE and
> Arch-family users should stay on the **0.3.1 broad release**, published on
> the public `maint/0.3` branch and the `v0.3.1` tag. Do not upgrade those hosts
> to 0.4.0.

## What v-EMU does

### Virtual machines

- Create VMs from ISO, import OVA/qcow2 images, or adopt existing libvirt VMs.
- Start, stop, force-off, edit hardware, change media and inspect XML drift.
- Embedded noVNC, external TigerVNC and SPICE consoles; mini console in the
  inspector.
- USB passthrough, guest clipboard, VirtioFS shares and runtime statistics.
- Read-only protection for external and VFIO/passthrough domains.

### Templates, clones and labs

- **Seal → clone → lab** workflow with immutable templates and linked overlays.
- Linux cloud-init and Windows OOBE clone paths.
- Declarative `lab.yaml` labs with `vemu up`, `vemu status` and `vemu down`.
- Portable lab bundles for moving a complete lab between hosts.

### Storage and snapshots

- Visible qcow2 chains with v2 metadata; no hidden layers.
- Offline snapshots you can list, revert and delete.
- **Compact chain** merges the active branch and reclaims host space.
- Storage roles for labs, templates and ISOs; reference-checked deletion and
  garbage collection.
- Permission, ownership and label repair from Storage or `vemu doctor`.

### Networking

- NAT, isolated and LAN-bridge profiles applied through libvirt and nftables.
- DHCP ranges, static reservations, port forwards and multiple NICs.
- Network map with live guest/interface visibility.

### Windows seamless mode

- Prepare a Windows 10/11 Pro/Enterprise guest for RemoteApp/RAIL publishing.
- Open selected Windows programs as Linux desktop shortcuts and manage the
  published program list.
- **Pause** stops publishing but keeps the guest, RDP/SSH setup and checkpoint.
- **Remove and restore** returns to the pre-seamless checkpoint and removes host
  state. Requires FreeRDP >= 3.26.
- See [`docs/WINDOWS-SEAMLESS.md`](docs/WINDOWS-SEAMLESS.md).

### Image Builder

- Build qcow2 lab images and branded installer ISOs with upstream
  `image-builder`.
- Blueprint import, package lookup, rootless preflight, build log and artifact
  handling.
- Installer branding and optional first-boot payloads.
- **Fedora/RHEL-family 10.3 hosts only.** Other host families use the 0.3.1
  broad release, which keeps the core features but not Image Builder.

### Operations and host security

- Full CLI parity for VM, template, storage, network, snapshot and lab work.
- Root `vemud` daemon on a local Unix socket; no telemetry or TCP listener.
- Doctor checks for KVM, tools, firmware, storage, SELinux and USB.
- VM delete cleans host shortcuts/icons, supervisor state and keyring entries.
- Doctor reports experimental 3D host security and can restore `qemu.conf`.

### Experimental Windows 3D (CLI only)

`vemu experimental 3d` is a separate, unsupported experiment for a virtio-gpu
3D Windows guest. It is **console/SPICE-only**, requires test signing and Secure
Boot off, and changes host `/etc/libvirt/qemu.conf` with explicit consent. It
is not a path for RDP, RemoteApp or seamless mode. See
[`docs/WINDOWS-3D-PREVIEW.md`](docs/WINDOWS-3D-PREVIEW.md).

## Host support

| Host | Status |
|---|---|
| Fedora 45 | Primary, full feature; KDE Plasma, GNOME and COSMIC UI tested |
| AlmaLinux 10.3 | Primary, full feature |
| RHEL 10.3 | Primary, full feature |
| AlmaLinux 9.9 | Optional EL9 installation-test path via `contrib/setup-el9.sh`; not a primary tier |

Fedora/RHEL-family 10.3 is the only supported host family in 0.4.0. The
Debian/Ubuntu, openSUSE/SUSE and Arch paths were removed from this tree.

**Users of the removed host families must stay on 0.3.1.** The broad 0.3.1
release remains available on the public [`maint/0.3`](https://github.com/withcoresystem/v-EMU/tree/maint/0.3)
branch and the `v0.3.1` tag, including its Debian/Ubuntu, openSUSE/SUSE and
Arch host support. Do not upgrade those hosts to 0.4.0; no further host fixes
are planned there.

Guest OS support in the VM wizard and templates is unchanged. AlmaLinux 9.9 is
RHEL-family but late in its lifecycle; it is an optional installation-test path
through `contrib/setup-el9.sh`, not a primary tier. The detailed policy is in
[`docs/SUPPORT.md`](docs/SUPPORT.md).

## Requirements

- 64-bit Linux with KVM: Intel VT-x (`vmx`) or AMD-V (`svm`), `/dev/kvm`.
- Python 3.11 or newer on the host.
- `sudo` for host packages, the daemon, groups and firmware.
- A Wayland desktop for the UI (KDE Plasma, GNOME and COSMIC tested).
- 20 GiB free for labs plus VM images.
- For seamless mode: Windows 10/11 Pro/Enterprise and FreeRDP >= 3.26.

There is no TCG-only host mode; cross-architecture TCG is optional for guests.

## Install

From source:

```bash
git clone https://github.com/withcoresystem/v-EMU vEMU
cd vEMU
./setup.sh --precheck
./setup.sh --standard        # x86_64 host
./setup.sh --full            # also aarch64/riscv64 TCG packages/firmware
```

Run as your normal desktop user, not with `sudo`. `--full` adds optional
aarch64/riscv64 TCG packages where the distribution provides them. Fedora 45
ships those packages; AlmaLinux and RHEL 10.3 default repositories do not, so
those hosts stay on `--standard` unless the cross-architecture packages are
added separately. After the first install, reboot or log out/in so group
membership applies.

Or download the matching `vemu-<version>-x86_64.run` from
[GitHub Releases](https://github.com/withcoresystem/v-EMU/releases) when the
tag is published, verify it with the adjacent `.sha256`, then run it. The
`.run` uses the same setup logic and keeps its extracted source under
`~/.local/share/vemu/source`; `--uninstall` removes it. For the broad 0.3.1
release, use the `v0.3.1` release asset and the `maint/0.3` branch.

Full install, first-run, adoption, support matrix and uninstall:
[`docs/MANUAL.md`](docs/MANUAL.md).

## Quick start

```bash
vemu --version
vemu doctor
vemu ui
```

Create lab storage and a NAT network from the first-run wizard, or from
**Storage** and **Networks**. Then use **Virtual machines → New VM**.

## Roadmap

The roadmap is directional, not a release promise.

### 0.5.0 — planned focus

- **Bug fixes** — triage 0.4.x regressions and user-reported issues on Fedora
  45 and Alma/RHEL-family 10.3, prioritizing VM lifecycle, console, storage,
  networking and seamless recovery.
- **UI/UX improvements** — cleaner everyday workflows, error and empty states,
  dialog consistency, keyboard navigation and accessibility; no new VM, guest
  or host features.
- **CLI autocomplete** — shell completion for the `vemu` command tree,
  including domains, storage roles, networks and common flags, starting with
  bash and zsh.

0.5.0 does not re-open the removed host families; those users stay on the 0.3.1
broad release.

## 0.4.0 — host-support narrowing

- Fedora 45 and Alma/RHEL-family 10.3 are the only supported hosts.
- Debian/Ubuntu, openSUSE/SUSE and Arch host paths, docs and tests are removed
  from this tree.
- `setup.sh` is a single dnf path; AlmaLinux 9.9 uses `contrib/setup-el9.sh`.
- Image Builder follows the host scope; the separate host-family disable gate
  and AppArmor host detection are removed.
- No new VM, guest, console, seamless or 3D features.
- The broad 0.3.1 release stays on `maint/0.3` for the removed host families.

## 0.3.1 patch

- The **Delete VM** dialog is short and context-aware: it only warns when the
  VM is running and explains the two storage choices.
- The in-app Guide now documents seamless/3D host-state cleanup during delete.
- No runtime feature changes; this is a small UI/docs patch on the 0.3.0 base.

## 0.3.0 highlights

- Windows seamless mode with pause and pre-seamless restore.
- Active-chain compaction and host-space reclamation.
- VM delete cleanup for shortcuts, host state and keyring entries.
- Doctor host-security status and guarded 3D qemu.conf restore.
- Experimental Windows 3D remains CLI-only and unsupported.

## Documentation

| Document | Contents |
|---|---|
| [`docs/MANUAL.md`](docs/MANUAL.md) | Install, daily use, adoption, networking, storage, seamless, security, CLI |
| [`docs/BUILDER.md`](docs/BUILDER.md) | Image Builder qcow2/ISO flow, blueprints, firstboot payloads |
| [`docs/BUILDER-BRANDING.md`](docs/BUILDER-BRANDING.md) | Installer ISO branding and asset standards |
| [`docs/GRAPHICS.md`](docs/GRAPHICS.md) | Video models, sleep/resume and guest trade-offs |
| [`docs/USER-GUIDE.md`](docs/USER-GUIDE.md) | Concise in-app guide |
| [`docs/WINDOWS-SEAMLESS.md`](docs/WINDOWS-SEAMLESS.md) | Seamless prepare, program management, pause, remove and recovery |
| [`docs/WINDOWS-3D-PREVIEW.md`](docs/WINDOWS-3D-PREVIEW.md) | Experimental Windows 3D: prerequisites, risks and rollback |
| [`docs/SUPPORT.md`](docs/SUPPORT.md) | Host matrix, support policy and removed host families |
| [`docs/ALMA-9.9.md`](docs/ALMA-9.9.md) | Optional EL9 installation-test helper and FreeRDP source build |
| [`docs/RELEASING.md`](docs/RELEASING.md) | Public `.run` build, clean-host test and GitHub release process |
| [`docs/release-notes/v0.4.0.md`](docs/release-notes/v0.4.0.md) | 0.4.0 host-support narrowing notes |
| [`docs/release-notes/v0.3.1.md`](docs/release-notes/v0.3.1.md) | 0.3.1 patch notes |
| [`docs/release-notes/v0.3.0.md`](docs/release-notes/v0.3.0.md) | 0.3.0 release notes and validation |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Issues, pull requests and scope |
| [`SECURITY.md`](SECURITY.md) | Vulnerability reporting and trust model |
| [`CREDITS.md`](CREDITS.md) | Maintainer, organization and upstream credit |
| [`DISCLAIMER.md`](DISCLAIMER.md) | Warranty, data/backup and liability terms |

## Non-goals

- multi-host cluster, scheduler, HA or live migration;
- remote/web multi-user operation or a remote thick client;
- supported 3D acceleration, commercial vGPU/GPU partitioning, or managed
  PCIe/VFIO passthrough. The Windows 3D preview is a separate CLI experiment,
  not support for passthrough or commercial vGPU;
- macOS guests;
- live snapshots (snapshots are offline, chain-based);
- X11 guest clipboard/integration;
- per-app RDP sessions / multi-session Windows clients as an icon-grouping
  workaround; v-EMU keeps one shared RAIL session per Windows VM;
- container management or orchestration (Docker, Podman, Kubernetes);
- arbitrary storage/network backends;
- a general-purpose replacement for Proxmox/oVirt/Incus.

## Security

The daemon runs as root and is the trust boundary for libvirt, storage and
network operations. Do not run the UI as root. There is no telemetry or update
service; `vemud` has no TCP listener, and Internet access happens only during
explicit Image Builder or package-lookup actions (plus a firstboot URL fetched
by the guest itself). See [`SECURITY.md`](SECURITY.md).

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE). Firmware and bundled assets have
their own notices in [`NOTICE`](NOTICE) and `firmware/`.
