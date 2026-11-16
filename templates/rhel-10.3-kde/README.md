# RHEL 10.3 KDE Plasma template

RHEL 10.3 + EPEL 10 KDE Plasma for vEMU labs.

ISO variant: `blueprint-iso.toml` (installer ISO; no fixed user, hostname or
qcow2 graphical-default unit). It adds `python3` and the Plymouth packages for
the optional first-boot TUI and boot branding. Use **New ISO build** in vEMU.

RHEL does not ship KDE in BaseOS/AppStream. EPEL 10 provides Plasma, and CRB
provides several runtime/build dependencies. On a registered RHEL 10.3 host:

```bash
sudo subscription-manager repos \
  --enable codeready-builder-for-rhel-10-x86_64-rpms
sudo dnf install -y https://dl.fedoraproject.org/pub/epel/epel-release-latest-10.noarch.rpm
```

## image-builder and CRB

`image-builder 79` on RHEL 10.3 only embeds BaseOS/AppStream, not CRB. Create a
repository override before building:

```bash
python3 prepare-crb-override.py
```

The script writes:

```text
~/.config/vemu/image-builder/repositories/rhel-10.3.json
```

vEMU picks this directory up automatically. For manual CLI builds, pass it
explicitly:

```bash
image-builder build qcow2 --distro rhel-10.3 \
  --blueprint blueprint.toml \
  --extra-repo https://dl.fedoraproject.org/pub/epel/10/Everything/x86_64/ \
  --force-repo-dir ~/.config/vemu/image-builder/repositories \
  --output-dir ./out
```

QEMU-optimized variant:

```bash
image-builder build qcow2 --distro rhel-10.3 \
  --blueprint blueprint-qemu.toml \
  --extra-repo https://dl.fedoraproject.org/pub/epel/10/Everything/x86_64/ \
  --force-repo-dir ~/.config/vemu/image-builder/repositories \
  --output-dir ./out
```

## Notes

- KDE login manager is `plasmalogin` (Plasma Login Manager), not SDDM.
- The built image needs its own `subscription-manager register` for RHEL repos.
  EPEL remains usable without a subscription, but RHEL BaseOS/AppStream updates
  need one.
- Do not redistribute RHEL images.
