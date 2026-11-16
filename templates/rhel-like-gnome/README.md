# RHEL-like GNOME qcow2 (AlmaLinux 10.2)

Minimal GNOME desktop for vEMU labs, built with `image-builder qcow2`.
AlmaLinux GNOME comes from the base repositories.

ISO variant: `blueprint-iso.toml` (installer ISO; no fixed user, hostname or
qcow2 graphical-default unit). It adds `python3` and the Plymouth packages for
the optional first-boot TUI and boot branding. Use **New ISO build** in vEMU.

```bash
image-builder manifest qcow2 --distro almalinux-10.2 --blueprint blueprint.toml
image-builder build qcow2 --distro almalinux-10.2 \
  --blueprint blueprint.toml --output-dir ./out
```

For Rocky/CentOS/Oracle, change `distro` to the matching image-builder name.
RHEL itself needs subscription/entitlement and is not a 0.2.0 target.

Optional blocks at the bottom: EPEL/CRB, RPM Fusion EL10, and firstboot
debloat. Uncomment only what you need, and add the matching `--extra-repo`
flags for build-time package resolution.
