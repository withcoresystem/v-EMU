# RHEL-like KDE qcow2 (AlmaLinux 10.2)

Minimal KDE desktop for vEMU labs, built with `image-builder qcow2`.
AlmaLinux base does not ship KDE; EPEL and CRB are required.

ISO variant: `blueprint-iso.toml` (installer ISO; no fixed user, hostname or
qcow2 graphical-default unit). It adds `python3` and the Plymouth packages for
the optional first-boot TUI and boot branding. Use **New ISO build** in vEMU.

```bash
image-builder manifest qcow2 --distro almalinux-10.2 --blueprint blueprint.toml \
  --extra-repo https://repo.almalinux.org/almalinux/10.2/CRB/x86_64/os/ \
  --extra-repo https://dl.fedoraproject.org/pub/epel/10/Everything/x86_64/

image-builder build qcow2 --distro almalinux-10.2 \
  --blueprint blueprint.toml --output-dir ./out \
  --extra-repo https://repo.almalinux.org/almalinux/10.2/CRB/x86_64/os/ \
  --extra-repo https://dl.fedoraproject.org/pub/epel/10/Everything/x86_64/
```

For Rocky/CentOS/Oracle, change `distro` to the matching image-builder name.
RHEL itself needs subscription/entitlement and is not a 0.2.0 target.

Optional blocks at the bottom: RPM Fusion EL10 and firstboot debloat. If you
uncomment RPM Fusion, add its `--extra-repo` URL as well.
