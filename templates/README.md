# vEMU desktop image blueprints

Native `image-builder` blueprints for the vEMU 0.3.0 desktop qcow2 and
installer ISO paths. The full workflow, branding and firstboot guide is
[`docs/BUILDER.md`](../docs/BUILDER.md).

These are **starting points**, not fixed policies. Edit, delete or uncomment
any block. vEMU does not invent a custom blueprint schema; the files are plain
TOML understood by `image-builder`.

## Templates

| Directory | Target | Image type |
|---|---|---|
| `fedora-kde/` | Fedora 45 KDE | `generic-qcow2` |
| `fedora-gnome/` | Fedora 45 GNOME | `generic-qcow2` |
| `rhel-like-kde/` | AlmaLinux 10.2 KDE | `qcow2` |
| `rhel-like-gnome/` | AlmaLinux 10.2 GNOME | `qcow2` |
| `alma-10.3-gnome/` | AlmaLinux 10.3 GNOME | `qcow2` |
| `rhel-10.3-gnome/` | RHEL 10.3 GNOME server | `qcow2` |
| `rhel-10.3-kde/` | RHEL 10.3 KDE Plasma (EPEL + CRB) | `qcow2` |

`rhel-like-*` is tested against AlmaLinux 10.2. For Rocky/CentOS/Oracle,
change `distro` to the matching image-builder name.

RHEL 10.3 requires a **registered RHEL host** with BaseOS/AppStream (and CRB
for KDE). Build on that host; the Fedora vEMU host cannot access RHEL
subscription sources. See the README in each RHEL directory. RHEL images are
subscription-bound and must not be redistributed.

Each directory also has a `blueprint-qemu.toml` variant. It adds
`vemu-debloat.service`, which removes firmware packages not used by QEMU/KVM
on first boot. vEMU exposes this as the **QEMU-optimized** image profile.
Physical passthrough and bare-metal hardware support are intentionally lost;
reinstall `linux-firmware` if needed.

Each directory also has a `blueprint-iso.toml` variant for the installer ISO.
It removes qcow2-only customizations that conflict with the interactive
kickstart flow: fixed user, hostname, root filesystem size, services, kernel
arguments and the qcow2 graphical-default unit. It adds `python3` for the
optional first-boot TUI and `plymouth`, `plymouth-scripts` and
`plymouth-plugin-two-step` for the branded boot screen on the installed system.
Use **New ISO build** in vEMU; the qcow2 wizard intentionally hides
`*-iso.toml` blueprints.

The ISO wizard adds two optional layers on top of the blueprint:

- **Branding** — Anaconda sidebar/header, logo, product title, GRUB entries and
  a Plymouth theme for the installed OS. The SVG is fitted to both `150x69`
  and `280x240`; it must have a transparent background and readable contrast.
- **First-boot TUI** — a one-shot TUI on `tty2` that runs a payload from a
  `VEMU-FBOOT` USB stick or an HTTP(S) URL.

Detailed rules, payload contract and the verification checklist are in
[`docs/BUILDER-BRANDING.md`](../docs/BUILDER-BRANDING.md).

## Validation status

The lab has also built and exercised additional GNOME/KDE qcow2 and installer
ISO variants beyond the detailed matrix below. `image-builder list` can expose
a distro/image type whose base image or repository is not published or
reachable yet; preflight reports the missing base/repo/package, and that is an
upstream availability condition, not a v-EMU bug. Re-run the matrix when the
repository is ready.

AlmaLinux 10.3 is available; `alma-10.3-gnome/` was built and boot-tested
(imported as `Alma-10.3-GNOME`, cloned, GDM/GNOME Shell 49.5, debloat done).
Rocky Linux 10.3 was not listed yet at the last check, and RHEL requires
subscription/entitlement; treat both as upstream availability, not as missing
v-EMU support.

Validated on Fedora 45 with `image-builder 89.0.0` / `osbuild 193`:

| Template | Manifest | Build | Boot test |
|---|---|---|---|
| `alma-10.3-gnome` | pass | 1.7 GB | **pass**: GDM, GNOME Shell 49.5, `graphical.target`, debloat done |

Validated on Fedora 45 with `image-builder 87.0.0` / `osbuild 193`:

| Template | Manifest | Build | Boot test |
|---|---|---|---|
| `fedora-kde` | pass | 2.6 GB | boot test with QXL reached greeter; use `--video virtio` for desktop |
| `fedora-gnome` | pass | 1.8 GB | lab build/boot exercised; detailed pass not recorded here |
| `rhel-like-kde` | pass | 2.1 GB | **pass with `--video virtio`**: Plasma desktop, `sddm`, vEMU clone + debloat |
| `rhel-like-gnome` | pass | 1.6 GB | **pass with `--video virtio`**: GDM, GNOME Shell, `en_US.utf8`, fixed U+2236 colon, vEMU debloat |

Validated on a registered RHEL 10.3 host with `image-builder 79.0.0` /
`osbuild 191`:

| Template | Manifest | Build | Boot test |
|---|---|---|---|
| `rhel-10.3-gnome` | pass | 1.9 GB | pass: GDM, DHCP `enp0s3`, graphical target, firmware/kdump/mcelog debloat |
| `rhel-10.3-kde` | pass | 2.5 GB | pass: `plasmalogin`, KDE Plasma, DHCP, firmware/kdump/mcelog debloat |

RHEL KDE requires EPEL 10 and CRB. `image-builder 79` does not embed CRB;
use `templates/rhel-10.3-kde/prepare-crb-override.py` to create the repository
override. RHEL images are subscription-bound and are not redistributable.

`image-builder` 87 rejects `customizations.services.default_target`, even
though newer blueprint documentation lists it. Older `image-builder` 52 (the
version shipped by AlmaLinux 10.2 AppStream) rejects
`customizations.firstboot` as well. The templates therefore use
`customizations.kernel.append = "systemd.unit=graphical.target"` plus a
systemd oneshot unit, `vemu-graphical-default.service`, installed with
`customizations.files` and enabled with `customizations.services`. The unit
persists `graphical.target` on first boot and touches
`/var/lib/vemu/graphical-default.done`. This works on both image-builder 52 and
newer releases.

### Host compatibility

The templates can be built from a Fedora or RHEL-family host. On AlmaLinux 10.2
the AppStream repo provides `image-builder 52.1` / `osbuild 174`; the updated
`rhel-like-*` templates pass `image-builder manifest` there with CRB + EPEL
`--extra-repo` arguments. Debian, Ubuntu and openSUSE hosts are not supported
by Image Builder and the vEMU page disables new builds with an explanatory
notice.

PackageKit can exceed its default 45-second start timeout on a slow first boot.
The Fedora KDE test VM showed `packagekit.service: start operation timed out`
while the firstboot debloat transaction was running; a manual restart took
13 seconds and cleared the failed state. The templates now ship a drop-in:

```text
/etc/systemd/system/packagekit.service.d/10-vemu-timeout.conf
```

with `After=network-online.target` and `TimeoutStartSec=180`. This is a
first-boot robustness fix, not a debloat package removal.

## Video model

Use `--video virtio` for Linux KDE/GNOME desktop images. The vEMU import UI
already defaults to virtio. QXL remains the Windows/RemoteApp choice.

Fedora 45 and AlmaLinux 10.2 both ship `virtio_gpu_dri.so` but **no qxl DRI
driver**. With `--video qxl`, KWin Wayland logs:

```text
libEGL warning: failed to get driver name for fd -1
MESA-LOADER: failed to retrieve device information
libEGL warning: egl: failed to create dri2 screen
```

and the console can show a black or flickering screen. The same Alma KDE image
with `--video virtio` renders the Plasma desktop normally (verified with a
QEMU screenshot). The QXL test was an explicit CLI import; the vEMU import UI
defaults to virtio and avoids this.

## Build

Run as root on a Fedora/RHEL-family host with `image-builder` installed.
`manifest` is rootless and is the recommended pre-build check.

```bash
# Fedora
image-builder manifest generic-qcow2 --distro fedora-45 --blueprint blueprint.toml
image-builder build generic-qcow2 --distro fedora-45 \
  --blueprint blueprint.toml --output-dir ./out

# Fedora installer ISO
image-builder build minimal-installer --distro fedora-45 \
  --blueprint blueprint-iso.toml --output-dir ./out

# AlmaLinux KDE: EPEL + CRB are required by the KDE package set.
image-builder manifest qcow2 --distro almalinux-10.2 \
  --blueprint blueprint.toml \
  --extra-repo https://repo.almalinux.org/almalinux/10.2/CRB/x86_64/os/ \
  --extra-repo https://dl.fedoraproject.org/pub/epel/10/Everything/x86_64/
image-builder build qcow2 --distro almalinux-10.2 \
  --blueprint blueprint.toml --output-dir ./out \
  --extra-repo https://repo.almalinux.org/almalinux/10.2/CRB/x86_64/os/ \
  --extra-repo https://dl.fedoraproject.org/pub/epel/10/Everything/x86_64/

# AlmaLinux installer ISO variant
image-builder build image-installer --distro almalinux-10.2 \
  --blueprint blueprint-iso.toml --output-dir ./out \
  --extra-repo https://repo.almalinux.org/almalinux/10.2/CRB/x86_64/os/ \
  --extra-repo https://dl.fedoraproject.org/pub/epel/10/Everything/x86_64/
```

The ISO image type is derived from the distro: `fedora-*` uses
`minimal-installer`, RHEL-like distros use `image-installer`. ISO variants need
the same `--extra-repo` arguments as their qcow2 counterpart; without them the
KDE/EPEL packages of the Alma templates are not resolvable. Branding and the
first-boot TUI do not change the base boot chain or Secure Boot signatures.

## Build host notes

- KDE qcow2 templates request a 12 GiB root; GNOME requests 8 GiB. Keep the
  `customizations.filesystem` block unless the base image's default is known
  to be large enough. The ISO variants intentionally omit it; Anaconda asks
  for the destination disk.
- OSBuild mounts build trees under `/run/osbuild`. If `/run` is a small tmpfs,
  bind-mount a disk-backed directory over `/run/osbuild` (or use
  `image-builder build --in-vm`). The test host needed this for the KDE root.
- `image-builder build` runs as root; `manifest` does not.
- `customizations.locale` writes `/etc/locale.conf`, but it does not install
  locale data. AlmaLinux needs `glibc-langpack-en` for the template's
  `en_US.UTF-8`; both Alma blueprints include it. Without it, `dnf` prints
  `Failed to set locale, defaulting to C.UTF-8` and `locale -a` only shows
  `C`/`C.utf8`. If you change `languages`, add the matching `glibc-langpack-*`.

## vEMU import → clone → firstboot test

Fedora KDE was tested through the real vEMU flow. Use virtio for the Linux
desktop; the debloat flow itself is independent of the video model.

```bash
vemu template import --from fedora-45-generic-qcow2-x86_64.qcow2 \
  --dest <templates> --name fedora-kde-debloat \
  --os-family linux --firmware bios --machine q35 \
  --disk-bus virtio --nic-model virtio --video virtio

vemu clone --template <templates>/fedora-kde-debloat \
  --dest <labs> --name vemu-debloat-reset \
  --define --start --network NAT --memory 4096 --vcpus 2
```

The default clone identity reset (hostname, machine-id, SSH host keys) **does
not** remove the OSBuild `/var/local/osbuild-first-boot/.run` marker. The clone
booted, the firstboot debloat service ran, `plasma-welcome` and `kcharselect`
were removed, `/var/lib/vemu/debloat.done` and
`/var/log/vemu-debloat.log` were created, and `graphical.target` /
`plasmalogin` were active. The clone disk was a 77 MiB overlay on the 5.3 GB
template base; the template itself was unchanged.

The same flow passed with AlmaLinux 10.2 KDE:

```text
DEBLOAT="kdeplasma-addons plasma-workspace-wallpapers"
```

The firstboot service removed both packages (270 MB freed), wrote the marker
and log, and left `graphical.target` / `sddm` active with no failed units.
Alma KDE does not ship `packagekit.service`, so the PackageKit drop-in is
inert there. The CRB repository uses the unique id `vemu-crb` to avoid a
duplicate-id warning from the built-in `almalinux-crb.repo`.

AlmaLinux 10.2 GNOME also passed:

```text
DEBLOAT="gnome-characters gnome-clocks"
```

The firstboot service removed both (4.2 MB), `gdm`/`graphical.target` were
active, `glibc-langpack-en` supplied `en_US.utf8`, PackageKit started with the
drop-in, and `systemctl --failed` was empty. The GDM greeter and GNOME Shell
desktop rendered with `--video virtio`. The clone disk was a 106 MiB overlay on
the 3.1 GB template base.

GNOME uses U+2236 (ratio colon) in the clock and display settings. The minimal
image has no font covering it, so those colons render as boxes. Both GNOME
templates include `dejavu-sans-fonts` (1.3 MB) to provide the fallback glyph;
the fixed image shows `5:10 AM` and normal aspect ratios.

## Third-party repositories

`customizations.repositories` writes and enables a repo **in the final image**.
It does **not** make packages from that repo resolvable during the build. If a
package in the blueprint comes from a third-party repo, pass the same repo URL
to `image-builder` with `--extra-repo`.

The optional blocks at the bottom of each blueprint cover:

- Fedora: RPM Fusion Free / Nonfree
- RHEL-like: EPEL, CRB, RPM Fusion Free / Nonfree

When you uncomment a repo block, add the matching `--extra-repo` argument.
Do not use `--force-repo`; it replaces the base repositories.

## Debloat at first boot

The debloat block is commented out by default. It uses
`customizations.firstboot.scripts`, so it runs **once at first boot**, not at
build time. The image still contains the packages until that first boot.

Older hosts need a newer builder for this block: `image-builder 52` (AlmaLinux
10.2 AppStream) rejects `customizations.firstboot`. Either build from
Fedora/newer RHEL-family `image-builder`, or move the script to a systemd
oneshot unit like the graphical-default unit above.

The example script:

- filters the list with `rpm -q`;
- uses `dnf --disablerepo='*' remove` so it can work offline;
- logs to `/var/log/vemu-debloat.log`;
- writes `/var/lib/vemu/debloat.done`.

Test the list against the exact desktop group before shipping. The KDE list is
the tested tier-1 starting point; GNOME needs its own tested list.

## Defaults to change before sharing

- `lab` / `welcomeadmin`: replace the password hash or remove the user block.
- `hostname`, timezone (`UTC`) and locale (`en_US.UTF-8`).
- The Chromium home page policy.
- Any optional repo you enable.

`image-builder` may need root for the actual build; `manifest` does not.
Firstboot debloat is intended for disposable lab qcow2 images, not for
bare-metal ISOs.
