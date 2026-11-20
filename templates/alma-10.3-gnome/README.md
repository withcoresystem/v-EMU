# AlmaLinux 10.3 GNOME qcow2

Minimal GNOME desktop for vEMU labs, built with `image-builder qcow2`.
AlmaLinux GNOME comes from the base repositories.

ISO variant: `blueprint-iso.toml` (installer ISO; no fixed user, hostname or
qcow2 graphical-default unit). It adds `python3` and the Plymouth packages for
the optional first-boot TUI and boot branding. Use **New ISO build** in vEMU.

```bash
image-builder manifest qcow2 --distro almalinux-10.3 --blueprint blueprint.toml
sudo image-builder build qcow2 --distro almalinux-10.3 \
  --blueprint blueprint.toml --output-dir ./out
```

The manifest/preflight is rootless; the real build needs root (pkexec or sudo).

For Rocky/CentOS/Oracle, change `distro` to the matching image-builder name when that release is published. RHEL itself is subscription-bound.
RHEL itself needs subscription/entitlement and is not a 0.2.0 target.

Optional blocks at the bottom: EPEL/CRB, RPM Fusion EL10, and firstboot
debloat. Uncomment only what you need, and add the matching `--extra-repo`
flags for build-time package resolution.
