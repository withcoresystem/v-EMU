# RHEL 10.3 GNOME template

RHEL 10.3 Server with GUI for vEMU labs and RHCSA/RHCE practice.

ISO variant: `blueprint-iso.toml` (installer ISO; no fixed user, hostname or
qcow2 graphical-default unit). It adds `python3` and the Plymouth packages for
the optional first-boot TUI and boot branding. Use **New ISO build** in vEMU.

## Requirements

- A **registered RHEL 10.3 host** (Anaconda registration or
  `subscription-manager register`).
- BaseOS and AppStream repos enabled.
- `image-builder`, `osbuild`, `osbuild-depsolve-dnf`, `qemu-img` installed.

The Fedora vEMU host cannot build this image by itself: RHEL sources use
`rhsm: true` and need a valid subscription. Build on the registered RHEL host,
then copy the qcow2 into the vEMU `images` folder if you want to import it as a
template there.

## Build

```bash
image-builder manifest qcow2 --distro rhel-10.3 --blueprint blueprint.toml
image-builder build qcow2 --distro rhel-10.3 \
  --blueprint blueprint.toml --output-dir ./out
```

QEMU-optimized variant (removes firmware not used by virtual hardware on first
boot, ~440 MiB):

```bash
image-builder build qcow2 --distro rhel-10.3 \
  --blueprint blueprint-qemu.toml --output-dir ./out
```

## Notes

- The built qcow2 is a new system. It does **not** inherit the host's
  subscription. Register it inside the VM if you need RHEL repos or updates:
  `subscription-manager register`.
- Do not redistribute RHEL images. The blueprint is fine to share; the built
  image is subscription-bound.
- If you hit `Signature check failed` / release key 4 on older RHEL 10.1/9.7,
  update `osbuild-composer` or use a repository override. RHEL 10.3 should
  include the current keys; verify on the build host.
