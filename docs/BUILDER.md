# vEMU Image Builder guide

This is the detailed guide for **Image Builder**: the qcow2 and installer-ISO
flows, blueprints, package lookup, branding, post-install payloads and
troubleshooting.

For normal vEMU use, start with [`MANUAL.md`](MANUAL.md) or the in-app guide
[`USER-GUIDE.md`](USER-GUIDE.md). This file goes deeper and is meant
for people who create or maintain images.

> **Status:** Image Builder orchestrates the upstream `image-builder` CLI on
> the Fedora 45 and Alma/RHEL-family 10.3 hosts. Other host families use the
> 0.3.1 broad release on `maint/0.3`.

## 1. What Image Builder is

- A **local orchestrator** around `image-builder`/osbuild. vEMU does not invent
  a second image format or a custom blueprint schema.
- **UI-first.** Open **Image Builder** from the main navigation. Catalogs,
  package lookup, preflight and build jobs are in that page.
- **One build at a time.** A build is an explicit user action with an explicit
  root consent prompt.
- **Local-only.** There is no upload, remote builder or image hosting.
- Blueprints are native TOML files; the templates under
  [`templates/`](../templates/) are starting points, not fixed policy.

### Supported hosts and targets

| Host | Image Builder |
|---|---|
| Fedora 45 | Supported |
| AlmaLinux/RHEL 10.3 (and compatible Rocky/CentOS/Oracle 10) | Supported, with repository/subscription prerequisites |

Targets come from `image-builder list` on the host. The catalog is cached
locally; **Refresh** asks the installed `image-builder` again.

On 2026-11-25, RHEL 10.3 with `image-builder` 79.0.0 and `osbuild` 191 passed
the complete `rhel-10.3` GNOME qcow2 flow: the catalog entry was present, the
subscription-aware preflight resolved 1241 packages, and the build produced a
1.9 GiB qcow2 in 26m47s.

### Main prerequisites

| Requirement | Why |
|---|---|
| `image-builder`, `osbuild`, `osbuild-depsolve-dnf` | Build and preflight |
| `qemu-img`, `rpm`, `dnf` | Image inspection and RPM-family builds |
| `pkexec` or `sudo` | Explicit root prompt for the build |
| Free space for the build tree and output | qcow2 is measured in GiB; the ISO adds a base ISO copy |
| Network access to the selected repositories | Package resolution and download |
| RHEL subscription/CRB where applicable | RHEL and Alma KDE package sets |

ISO builds additionally need `mkksiso`, `xorriso`, `cpio`, ImageMagick
(`magick` or `convert`), `fc-match` and a sans font. Branding and firstboot
preflight report missing tools.

The **Recheck** button on the Image Builder page re-runs host/tool detection.
The OSBuild catalog, package lookup and preflight use the same host checks.

## 2. Choose the flow

| | qcow2 image | Installer ISO |
|---|---|---|
| Wizard | Mode → Blueprint → Preflight → Build | Blueprint → Branding → Firstboot → Preflight → Build |
| Blueprint | `blueprint.toml` or `blueprint-qemu.toml` | `blueprint-iso.toml` |
| Output | Bootable virtual disk for labs/templates | Bootable installer media |
| Branding | Not applicable | Anaconda, GRUB and installed Plymouth |
| Post-install | Firstboot debloat unit in the QEMU profile | Optional firstboot TUI payload |
| Typical use | `vemu` template → clone | Install on a VM or bare metal |

**qcow2 is the normal vEMU path.** The installer ISO is an Advanced output for
cases where a normal Anaconda/installer flow is required.

## 3. qcow2 flow

```text
Image Builder → New qcow2 build
  1. Mode        Image (qcow2); Full or QEMU-optimized
  2. Blueprint   import a native TOML blueprint
  3. Preflight   manifest, packages/repos, disk, labels, privilege
  4. Build       output directory + root consent
```

### 3.1 Mode and profile

- **Image (qcow2)** is the default. The output is a qcow2 disk, not an ISO.
- **Full** keeps the template package set, including firmware.
- **QEMU-optimized** adds the `vemu-debloat.service` firstboot unit. It removes
  firmware packages that QEMU/KVM does not need on the first boot and writes
  `/var/lib/vemu/debloat.done` plus `/var/log/vemu-debloat.log`. The image still
  contains those packages until that first boot finishes. Do not use this
  profile for physical passthrough or bare-metal deployment; reinstall
  `linux-firmware` if the image is later moved to hardware.

The current wizard builds for the host architecture. Cross-architecture image
building is not part of this flow; guest TCG is a separate feature.

### 3.2 Blueprint

Import a TOML file from [`templates/`](../templates/) or your own directory. The
Blueprint tab shows the name, version and file hash. Importing does not modify
the file and does not download anything.

Use the `blueprint-qemu.toml` variant if you want the QEMU debloat profile.
Selecting **QEMU-optimized** expects that variant next to the base blueprint;
if it is missing, the wizard reports it instead of silently building the base
image.

### 3.3 Preflight

Preflight is read-only and runs `image-builder manifest` as your user. It checks:

- required host tools;
- blueprint syntax and package/group resolution;
- repository reachability and missing packages;
- output space and the managed `images` role;
- SELinux labeling for qcow2 output;
- the build lock and privilege method.

Fix blocking errors before building. Warnings are shown so they can be
understood, not silently ignored.

### 3.4 Build and result

The build runs as root after consent. The build window is modeless:

- closing it hides the window; it does not cancel the job;
- reopening from **Images** shows the running job;
- **Cancel** kills the process group after confirmation;
- quitting the main app while a build runs asks to keep vEMU open or cancel the
  build and quit.

Successful qcow2 jobs keep the final artifact and `build.log`; intermediate
work is removed. Failed/cancelled jobs keep their log and partial output for
diagnosis.

The artifact is placed under the managed **images** storage role and receives
the `virt_image_t` label. Use **Import as template** explicitly when you want a
template; vEMU never imports a build automatically.

## 4. Installer ISO flow

```text
Image Builder → New ISO build
  1. Blueprint   *-iso.toml only
  2. Branding    optional Anaconda/GRUB/Plymouth customization
  3. Firstboot   optional one-shot post-install TUI
  4. Preflight   tools, packages, branding, firstboot, space, privilege
  5. Build       base ISO + vEMU post-processing
```

The ISO wizard hides qcow2 blueprints. The qcow2 wizard hides `*-iso.toml`.

### 4.1 ISO blueprint

Use the `blueprint-iso.toml` variant. It should:

- remove qcow2-only fixed user, hostname, filesystem size, enabled services and
  kernel arguments;
- add `python3` if the first-boot TUI is enabled;
- keep `plymouth`, `plymouth-scripts` and `plymouth-plugin-two-step` when the
  branded boot screen is enabled;
- keep the desktop package set and repository declarations from the template.

Do not put a fixed user, hostname or installer-breaking kernel arguments in an
ISO blueprint: Anaconda asks for the destination and user at install time.

Image type is derived from the distro:

```text
fedora-*                    -> minimal-installer
rhel, almalinux, rocky,
centos, oracle              -> image-installer
```

### 4.2 Branding layer

Branding affects:

- **Anaconda:** sidebar colour, logo, header accent and product title/version.
- **GRUB:** menu entry titles and timeout; the default entry stays first.
- **Installed system Plymouth:** logo, fixed credit `Powered by v-EMU`,
  spinner, no boot logs.
- **Optional firstboot TUI:** see section 5.

It does **not** modify the base boot chain, shim/kernel signatures or Secure
Boot behavior. Branding is supported on Fedora/RHEL-like Anaconda GTK ISOs,
not non-RHEL installers.

### 4.3 Firstboot layer

Optional one-shot TUI on `tty2` before the display manager. It can run a payload
from a USB stick, an HTTP(S) URL, or a payload bundled into the ISO. See
section 5.

### 4.4 Preflight and build

ISO preflight adds branding/firstboot validation, ISO tools, Plymouth/runtime
warnings and privilege checks to the normal checks. Blocking errors stop the
build.

The base ISO is built by `image-builder`; vEMU then injects the kickstart,
`updates.img`, GRUB replacements and optional firstboot files with `mkksiso`.
The final ISO is written to the path you choose. It is **not** registered,
relabeled or deleted by vEMU; **Remove from list** drops only the job record.

### 4.5 Manual upstream commands

For debugging outside vEMU, the same upstream commands are:

```bash
# qcow2
image-builder manifest generic-qcow2 --distro fedora-45 \
  --blueprint blueprint.toml
image-builder build generic-qcow2 --distro fedora-45 \
  --blueprint blueprint.toml --output-dir ./out

# installer ISO
image-builder build minimal-installer --distro fedora-45 \
  --blueprint blueprint-iso.toml --output-dir ./out
```

RHEL-like KDE templates need their CRB/EPEL repositories passed as
`--extra-repo` to both commands. vEMU can derive them from the blueprint's
`customizations.repositories`, but a template with a repository that is not
declared there must be resolved manually.

## 5. Post-install scripts and firstboot

There are three different mechanisms; do not mix them up.

### 5.1 QEMU debloat unit in the qcow2 profile

The **QEMU-optimized** profile adds `vemu-debloat.service` to the image. It
runs once at first boot, not at build time. It removes firmware/kdump packages,
writes markers and logs, and does nothing on later boots. The image still
contains those packages until that first boot completes.

This is not a general script hook and is not a reason to add arbitrary scripts
to a qcow2 blueprint.

### 5.2 ISO firstboot TUI

The ISO wizard can enable a one-shot TUI. Sources:

| Source | Meaning |
|---|---|
| USB | vfat stick labelled `VEMU-FBOOT` (11-character label limit) |
| URL | HTTP(S) script or `.tar`/`.tar.gz` archive |
| Bundled | Reserved for payloads baked into the ISO; not exposed by the wizard |

The TUI writes `/var/lib/vemu/firstboot.done` on success or
`firstboot.skipped` after a confirmed quit, logs to
`/var/log/vemu-firstboot.log`, and removes its unit/config/payload afterwards.
`retries` is the total attempt limit (0–20), not extra retries after failure.

USB layout:

```text
VEMU-FBOOT/
  vemu-firstboot/
    run.sh            executable entry point, or any *.sh
    manifest.toml     optional
    ...               extra files
```

`manifest.toml`:

```toml
run = "setup.sh"     # relative path inside the payload
timeout = 900        # seconds; 0 or omitted means no timeout
```

URL payloads: a direct script is saved as `run.sh`; a tar archive is extracted
with absolute paths and `..` members skipped.

### 5.3 Automatic firstboot payload

The ISO wizard can bake a fixed payload:

| Script name | Script URL | Behavior |
|---|---|---|
| empty | empty | Manual menu |
| `site-setup.sh` | empty | Try that file on the single `VEMU-FBOOT` USB stick |
| empty | direct file URL | Download the URL directly |
| `site-setup.sh` | base URL ending `/` | Download `<base>/site-setup.sh` |
| `site-setup.sh` | direct file URL | Rejected by validation |

Automatic mode skips the menu, tries URL first with USB fallback when a name is
set, and uses the same retry limit. Success shows a 10-second restart countdown;
failure returns to the manual menu with the log. Keep a fixed script name and
change the server content when possible, so the ISO does not need rebuilding.

### 5.4 Payload contract

- **Non-interactive:** no stdin.
- **Idempotent:** a retry can repeat partially completed work.
- **Bounded:** set `timeout` or make the script self-limiting.
- **Log to stdout:** that output is the progress screen and support log.
- **Fail loudly:** exit non-zero on the first real error; `set -euo pipefail`.
- **No reboot/poweroff:** vEMU owns the restart prompt.
- **No secrets:** the payload is world-readable on the ISO/USB side and the log
  is plain text.

Execution environment:

```text
VEMU_FIRSTBOOT=1
VEMU_PAYLOAD_SOURCE=usb|url|bundled
VEMU_PROFILE=<company name>
```

The payload runs as root, from its payload directory, with `/bin/bash <entry>`.
There is no signature or checksum validation: the USB stick or URL is trusted
input. Prefer HTTPS. A package-installing payload should use configured
repositories or verify its own downloads.

The example payload [`../examples/firstboot-payloads/post-setup.sh`](../examples/firstboot-payloads/post-setup.sh)
is a Fedora/RHEL-family starting point (dnf/rpm only).

### 5.5 qcow2 and arbitrary post-install scripts

`image-builder` 87 rejects `customizations.script`, and older 52 rejects
`customizations.firstboot`. A qcow2 image therefore has no portable arbitrary
script hook in the current templates. Use one of these instead:

- `customizations.files` plus a systemd unit enabled by
  `customizations.services` for a one-shot unit;
- the ISO firstboot TUI when the image is installed from an ISO;
- cloud-init or a firstboot payload after import, if your workflow supports it.

Do not publish a blueprint that claims a generic script hook will run during a
qcow2 build.

## 6. Blueprint guide

Blueprints are plain `image-builder` TOML. vEMU does not extend the schema.

A minimal qcow2 blueprint:

```toml
name = "lab-kde"
description = "Fedora KDE lab image"
version = "1.0.0"
distro = "fedora-45"

[[customizations.filesystem]]
mountpoint = "/"
minsize = 12884901888

[[groups]]
name = "kde-desktop"

[[packages]]
name = "qemu-guest-agent"
version = "*"

[[packages]]
name = "openssh-server"
version = "*"

[customizations]
hostname = "lab-kde"

[customizations.timezone]
timezone = "UTC"

[customizations.locale]
languages = ["en_US.UTF-8"]
keyboard = "us"

[customizations.services]
enabled = ["sshd", "qemu-guest-agent"]

[customizations.kernel]
append = "systemd.unit=graphical.target"
```

### 6.1 Packages and groups

- `[[packages]]` entries use `name` and `version = "*"`.
- `[[groups]]` entries use a group ID from the target distribution.
- Use **Package lookup** to search packages/groups before editing the file.
- Add `glibc-langpack-*` when you change `customizations.locale`; locale data is
  not installed by the locale customization alone.

### 6.2 Repositories

Declare extra repositories in the blueprint:

```toml
[[customizations.repositories]]
id = "example-extra"
name = "Example extra repository"
baseurls = ["https://example.invalid/repo/"]
gpgcheck = true
gpgkeys = ["https://example.invalid/RPM-GPG-KEY"]
enabled = true
```

The matching URL must also be passed to the build as `--extra-repo` when
required by the template. vEMU derives enabled repository URLs from
`customizations.repositories`, but package resolution is ultimately decided by
`image-builder manifest`.

AlmaLinux KDE needs CRB and EPEL. RHEL 10.3 needs a registered RHEL host and
may need CRB for KDE. RHEL images are subscription-bound and must not be
redistributed.

**Security:** `image-builder --extra-repo` repositories are not GPG-checked by
the tool. Use only trusted URLs and do not put credentials in a URL; it can
appear in the command line and build log.

### 6.3 Version differences

`image-builder` behavior varies by release:

- 52 (AlmaLinux 10.2 AppStream) rejects `customizations.firstboot`;
- 87 rejects `customizations.services.default_target`;
- the templates use `customizations.kernel.append =
  "systemd.unit=graphical.target"` plus a `vemu-graphical-default.service`
  oneshot unit, which works on both.
- `customizations.script` is not a portable qcow2 hook.

Run preflight against the exact host/image-builder version you will build on.

### 6.4 QEMU profile variant

`blueprint-qemu.toml` keeps the template package set but enables the
`vemu-debloat.service` firstboot unit. It is intended for QEMU/KVM lab images.
Physical passthrough and bare-metal hardware support are intentionally lost.
If you move the image to hardware, reinstall `linux-firmware` and review the
removed kdump/mcelog packages.

Keep the QEMU variant in sync when editing the full blueprint.

## 7. Package and group lookup

The Image Builder host row has **Package lookup** next to **OSBuild Catalog**.

- Select the target distro and architecture; the default follows the host.
- Search package names or globs. Separate multiple terms with commas:
  `inkscape, gimp, *fonts*`.
- Open **Groups** to list package groups from the target repositories and
  filter them with comma-separated terms.
- Enable **Show hidden groups** to include groups the distribution marks
  hidden. On Fedora 45 this changes the list from 49 to 155 groups (106 more).
- **Validate selected group** resolves the group and reports its package count.
- Right-click a package/group row to copy the name, ID, version or whole row.
- **Extra repos** accepts comma-separated URLs for CRB/EPEL/etc. They affect
  the lookup only and are not written into the blueprint.

Lookup is read-only: it never installs, builds or executes package scripts.
The first query for a target prepares repository metadata and may take a few
seconds. Results are cached in SQLite under `~/.cache/vemu`; later queries are
fast. Package results expire after seven days; catalog, repository and group
data after 90 days. **Refresh metadata** clears the target cache.

Metadata lookup disables GPG checking for its read-only queries, so results are
advisory. Builds still use the full `image-builder manifest` preflight.

## 8. Branding and assets

Branding is optional and applies only to the installer ISO. All inputs are
validated before the build starts.

### 8.1 Inputs

| Field | Default | Rule |
|---|---|---|
| Product name | `vEMU Lab` | Required; Anaconda shows it uppercase + `INSTALLATION` |
| Product version | `0.2` | Required; keep short |
| Sidebar colour | `#111827` | `#RRGGBB`; keep dark enough for light text |
| Accent | `#0f766e` | `#RRGGBB`; header and spinner tint |
| Logo SVG | bundled mark | Valid XML, `<svg>` root, at most 512 KiB |
| GRUB title | `Install <name>` | One line; short |
| GRUB timeout | 15 s | 0–600; fixed in the wizard |
| Firstboot company name | required if enabled | Used as `VEMU_PROFILE` |

### 8.2 Logo and asset standards

- **Transparent background.** A baked white background shows a box on the
  sidebar and boot gradient.
- **Square or slightly wide.** The SVG is fitted to `150x69` for the Anaconda
  sidebar and `280x240` for the Plymouth screen, preserving aspect ratio.
  Extremely wide/tall art becomes tiny.
- **No baked text.** The product name is set by Anaconda; the boot credit is
  fixed `Powered by v-EMU`.
- **Low detail.** Thin strokes and fine gradients disappear at 69 px high.
- **Contrast is your job.** vEMU does not recolour the SVG. Check it against
  both the sidebar colour and the dark boot gradient.
- Use `#RRGGBB` colours; three-digit hex and named colours are rejected.

Branding assets generated for Plymouth:

| Asset | Size | Purpose |
|---|---|---|
| `watermark.png` | `780x400` transparent | Logo (`280x240` max) plus credit |
| `progress-0001..0030.png` | `72x72` | Rotating accent spinner |
| `lock.png`, `entry.png`, `bullet.png` | generated | Password prompt widgets |
| `capslock.png`, `keyboard.png` | 1x1 transparent | Optional indicators |

The theme is a Plymouth `two-step` theme. If `lock.png`, `entry.png` or
`bullet.png` is missing, Plymouth silently falls back to the text splash, so
vEMU always generates them.

### 8.3 What is changed

```text
Anaconda:  updates.img (.buildstamp, anaconda-gtk.css, sidebar-logo.png)
GRUB:      mkksiso -R title replacements and timeout
Installed: Plymouth theme in the initramfs, graphical kernel arguments
Firstboot: optional tty2 oneshot service and payload
```

Secure Boot shim/kernel signatures from the base ISO are not changed. The
Anaconda Web UI, Calamares and Agama are not supported.

### 8.4 Asset checklist

1. SVG has an `<svg>` root, no external fonts/images, and is ≤512 KiB.
2. Transparent background; no baked text; readable at 69 px high.
3. Check contrast on both sidebar and boot gradient.
4. Product name/version are short; GRUB title is one line.
5. If firstboot is enabled, `python3` is in the ISO blueprint.
6. If Plymouth is branded, `plymouth-plugin-two-step` is present.
7. Run preflight and review warnings before building.

## 9. Output, jobs and storage

- **qcow2:** stored under the registered `images` role and labeled
  `virt_image_t`. Use **Import as template** explicitly, then clone.
- **ISO:** user-selected file, unmanaged; vEMU does not label, move or delete it.
  `Remove from list` only removes the job record.
- **Logs:** the job directory keeps `build.log`; successful jobs remove
  intermediate work. Failed/cancelled jobs keep partial output for diagnosis.
- **One job at a time.** Build history is local. Closing the build window hides
  it; quitting vEMU cancels the running build.
- A crash/logout/reboot does not reattach a build; the next start marks a stale
  job failed and preserves its log.

## 10. Network and security

Image Builder is not a background service. The catalog is local; preflight,
build and package lookup contact repositories only while you run them. There is
no telemetry or update checker.

- Preflight reads repository metadata; build downloads packages and runs as
  root after consent.
- Package lookup is advisory and never installs.
- `--extra-repo` is not GPG-checked; trust the URL.
- Do not put credentials in repository URLs.
- The firstboot URL is fetched by the installed guest at first boot, not by the
  host during the ISO build. USB payloads work offline.
- The SQLite metadata cache is a local copy of repository data and is safe to
  delete.

Full details: [`../SECURITY.md`](../SECURITY.md#runtime-network-access).

Air-gapped options: build with local repository overrides
(`VEMU_IMAGE_BUILDER_REPO_DIR`/`--force-repo-dir`), use cached lookup data, use
USB firstboot payloads, and attach guests to an isolated network.

## 11. Verification checklist

### qcow2

- [ ] Preflight has no blocking errors.
- [ ] Build job succeeds; artifact is under the `images` role.
- [ ] Import as template is explicit; clone boots to the desktop.
- [ ] Use `virtio` video for Linux KDE/GNOME images.
- [ ] QEMU guest agent/SSH work; the clock and network are correct.
- [ ] QEMU profile: `/var/lib/vemu/debloat.done` and
      `/var/log/vemu-debloat.log` exist after first boot; later boots do not
      repeat the debloat.
- [ ] Clone identity reset changes hostname/machine-id/SSH host keys.

### Installer ISO

- [ ] Anaconda shows the branded sidebar, logo, title and header colour.
- [ ] GRUB shows the branded entries, default entry first, configured timeout.
- [ ] Secure Boot remains enabled and the installed system boots.
- [ ] Plymouth shows the logo, `Powered by v-EMU`, spinner; no boot logs.
- [ ] `cat /proc/cmdline` contains the expected graphical arguments.
- [ ] `lsinitrd` contains the vEMU theme/spinner.
- [ ] USB firstboot: label detected, payload runs, log/marker written, unit
      removed.
- [ ] URL firstboot: DNS wait, HTTPS, retry, failure menu, success countdown.
- [ ] A failed payload leaves a readable log and can retry.

## 12. Troubleshooting

| Symptom | Likely cause / action |
|---|---|
| Image Builder page disabled | Host is not Fedora/RHEL-family, or tools are missing; use a supported build host |
| `image-builder` not found | Install `image-builder`/`osbuild`; press Recheck |
| Manifest fails on packages | Missing repository, package name, CRB/EPEL, or RHEL subscription |
| RHEL target fails | Build on a registered RHEL host or provide repository overrides |
| Alma KDE fails | Add CRB + EPEL `--extra-repo` URLs from the template README |
| Not enough space | Free disk; `/run/osbuild` may need a disk-backed bind mount |
| Build hangs on cancel | Root process group/helper issue; wait for the cancel confirmation and check the log |
| Black/flickering KDE desktop | QXL DRI is missing on Fedora/Alma; import with `virtio` video |
| PackageKit firstboot timeout | Template includes a 180 s drop-in; check `/etc/systemd/system/packagekit.service.d` |
| Locale errors | Add `glibc-langpack-<lang>` to the blueprint |
| Firstboot TUI missing | `python3`/`curses` missing in the installed image |
| USB stick not detected | Label must be `VEMU-FBOOT`, vfat, with `vemu-firstboot/run.sh` |
| URL payload fails | No DHCP/DNS, TLS error, plain HTTP blocked, or URL not a script/archive |
| Plymouth text splash | Missing generated theme assets or theme not in initramfs |
| Anaconda unbranded | `updates.img` not loaded or invalid product data |
| GRUB title unchanged | Base ISO entries did not match the `mkksiso -R` replacement |
| Build aborted after quitting vEMU | Builds do not detach; start again and keep vEMU open |

## 13. Templates and limits

The templates under [`templates/`](../templates/) cover Fedora 45 KDE/GNOME,
AlmaLinux 10.2 KDE/GNOME and RHEL 10.3 GNOME/KDE. Validation status and known
limits are in [`../templates/README.md`](../templates/README.md).

- RHEL images are subscription-bound and not redistributable.
- KDE/GNOME images need a sufficiently large root filesystem; keep the
  `customizations.filesystem` block.
- Linux desktop images use `virtio` video; QXL is a Windows/RemoteApp choice.
- Branding is Anaconda/GRUB/Plymouth only; no Calamares/Agama support.
- Firstboot payloads are trusted root scripts, not signed packages.
- Image Builder is UI-only; vEMU does not expose a separate image-build CLI.

## 14. Related documents

- [`MANUAL.md`](MANUAL.md) — install, daily use, CLI, networking, storage.
- [`USER-GUIDE.md`](USER-GUIDE.md) — concise in-app guide.
- [`../SECURITY.md`](../SECURITY.md#runtime-network-access) — outbound network and
  trust boundaries.
- [`../templates/README.md`](../templates/README.md) — template matrix and validation.
- [`BUILDER-BRANDING.md`](BUILDER-BRANDING.md) — full branding,
  Plymouth, firstboot and verification reference.
- [`../examples/firstboot-payloads/post-setup.sh`](../examples/firstboot-payloads/post-setup.sh)
  — example payload.
