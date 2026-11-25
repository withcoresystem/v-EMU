# Support policy

v-EMU is a **single-host, lab-first desktop application**. This page defines
what “supported” means, which hosts are tested, and what users can expect from
issues and releases.

> **0.4.0 narrows host support to Fedora and RHEL-family 10.3.** It adds no
> new VM, guest or UI features. Debian/Ubuntu, openSUSE/SUSE and Arch-family
> users must stay on the broad **0.3.1** release, available on the public
> `maint/0.3` branch and the `v0.3.1` tag. Do not upgrade those hosts to 0.4.0.

## 1. Support tiers

### Primary — Fedora and RHEL-family 10.3

| Host | Evidence |
|---|---|
| Fedora 45 | Full feature validation; KDE Plasma, GNOME and COSMIC UI tested |
| AlmaLinux 10.3 | Full feature validation |
| RHEL 10.3 | Full feature validation |

Image Builder is supported on these hosts. Seamless mode uses FreeRDP >= 3.26
from the distribution where available. SELinux is the supported host security
layer; AppArmor-specific detection and repair were removed in 0.4.0.

### Optional EL9 installation-test path

AlmaLinux 9.9 is RHEL-family but late in its lifecycle. It is **not a primary
tier**. Installation and feature smoke tests use `contrib/setup-el9.sh`; the
helper installs Python 3.11, CRB, `libvirt-devel`, the system
`libvirt-python` binding and Image Builder packages, then calls the unchanged
`setup.sh`. Seamless mode needs `contrib/setup-freerdp-el9.sh` (FreeRDP 3.33.1)
and was validated with a Notepad RAIL test. Treat EL9 as best-effort.

### Removed host families

Debian/Ubuntu, openSUSE/SUSE and Arch-family hosts are **not supported** in
0.4.0. They remain supported in the broad 0.3.1 release on the public
[`maint/0.3`](https://github.com/withcoresystem/v-EMU/tree/maint/0.3) branch;
pin the `v0.3.1` tag and do not upgrade those hosts to 0.4.0. This is a
host-support change only; guest OS support in the VM wizard and templates is
unchanged.

### Seamless mode client requirement

| Host | Seamless mode |
| --- | --- |
| Fedora 45 | Full validation with FreeRDP 3.33.1 |
| AlmaLinux 10.3 | RAIL core and input validated with FreeRDP 3.32.2 from AppStream |
| RHEL 10.3 | RAIL validated with FreeRDP 3.32.2 from AppStream on KDE Plasma Wayland through Xwayland |
| AlmaLinux 9.9 | Contrib build with FreeRDP 3.33.1; Notepad RAIL validated |

## 2. Host support is not guest support

The host matrix is only for **installing and running v-EMU**. The New VM wizard
supports more guest OS families and versions independently. A guest OS working
on a supported host does not make every host distribution supported.

## 3. What “supported” covers

On a primary host, the expected support scope is:

- `setup.sh --precheck` and `--standard`;
- daemon/service installation and CLI/UI launch;
- storage registration, ownership and label repair;
- VM create/start/stop, console and basic hardware editing;
- snapshots, v2 chains and templates/clones;
- migration/adoption flows;
- issues that can be reproduced with the latest `main` or latest release.

`--full` cross-architecture TCG is optional. It requires QEMU aarch64/riscv64
packages and firmware from the host repositories. Fedora 45 ships both;
AlmaLinux and RHEL 10.3 default repositories do not, so `--full` is not
available there unless those packages are added from another source.

## 4. What is not supported

v-EMU does not aim to be a general virtualization platform. The project does
not support:

- 3D acceleration as a supported feature, commercial vGPU/GPU partitioning
  (NVIDIA vGPU, AMD MxGPU, Intel GVT-g, GPU-P, SR-IOV, DDA), or managed
  PCIe/VFIO passthrough; a VM with a manually added PCI/mediated/SCSI hostdev is
  detected as read-only **External (VFIO)**. The Windows 3D preview is a
  separate 0.4.0 CLI-only experiment (test signing, Secure Boot off,
  SPICE-only, Experimental settings toggle), not support for passthrough or
  commercial vGPU. See
  [`WINDOWS-3D-PREVIEW.md`](WINDOWS-3D-PREVIEW.md);
- remote access, multi-user operation or a web UI;
- clustering, live migration or HA;
- macOS guests;
- multi-tenant/cloud lab orchestration;
- arbitrary storage/network backends;
- every Linux distribution or every future rolling-release snapshot.

These are product boundaries, not temporary backlog items. See the non-goals
in the README.

## 5. Issue expectations

- No SLA and no guaranteed response time.
- Use the issue template and include `vemu doctor --json`, host/version details,
  and a minimal reproduction.
- Redact secrets, internal IPs and VM data.
- Issues on unsupported hosts may be closed as out of scope.
- Security reports must use the private process in `SECURITY.md`.

## 6. Releases and versions

- Releases are ad-hoc, not nightly or time-boxed.
- The current `main` and latest tagged release are the supported versions.
- The broad 0.3.1 release is kept on `maint/0.3` for old host families; it is
  not the 0.4.0 support target.
- Source users install with `./setup.sh --standard`; the `.run` installer is
  published on GitHub Releases.
- A release is not automatically “stable”. Host support and known limitations
  are documented in this file and the release notes.

## 7. Contributions and support load

v-EMU is maintainer-led and deliberately low-frequency. Open an issue before a
large PR; unsolicited large changes may be declined. See `CONTRIBUTING.md`.

The maintainer prioritizes fixes that keep the primary host matrix and core
lab flows working, not feature breadth.

## 8. Guest scope

The host matrix above is separate from guest support. The New VM wizard and
templates cover:

- Windows Server 2025 (unattended answer media) and other Windows
  Server/client images (interactive install);
- Fedora/RHEL/Rocky/Alma, Debian/Ubuntu and openSUSE/SLE families;
- Other/BSD/unknown guests with safe fallback hardware;
- optional aarch64/riscv64 UEFI guests under TCG when host packages and
  firmware are available.

Guest-specific limits remain authoritative in [`MANUAL.md`](MANUAL.md),
[`GRAPHICS.md`](GRAPHICS.md) and [`ARCHITECTURE.md`](ARCHITECTURE.md).
Windows Sysprep is manual; Linux clone identity reset and cloud-init are
best-effort for supported families.

## 9. Historical evidence

Older per-host test logs and removed-host notes remain in the broad 0.3.1
release on `maint/0.3`. They document past tests and may mention removed
features; they are not part of the current support contract.
