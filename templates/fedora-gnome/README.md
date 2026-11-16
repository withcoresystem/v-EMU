# Fedora 45 GNOME qcow2

Minimal GNOME desktop for vEMU labs, built with `image-builder generic-qcow2`.

ISO variant: `blueprint-iso.toml` (installer ISO; no fixed user, hostname or
qcow2 graphical-default unit). It adds `python3` and the Plymouth packages for
the optional first-boot TUI and boot branding. Use **New ISO build** in vEMU.

```bash
image-builder manifest generic-qcow2 --distro fedora-45 --blueprint blueprint.toml
image-builder build generic-qcow2 --distro fedora-45 \
  --blueprint blueprint.toml --output-dir ./out
```

Edit the blueprint freely. Optional blocks at the bottom:

- RPM Fusion Free/Nonfree (uncomment the repo blocks and add the matching
  `--extra-repo` flags);
- firstboot debloat (runs once at first boot, not at build time).

Defaults to change before sharing: `lab` / `welcomeadmin`, hostname, timezone,
locale.
