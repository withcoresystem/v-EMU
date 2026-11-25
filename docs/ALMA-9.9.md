# AlmaLinux 9.9 host notes

**Status: optional installation-test path for the 0.4.0 line. Not a primary tier.**
AlmaLinux/RHEL 10.3 and Fedora 45 remain primary. 0.3.1 is unchanged; this
page describes the EL9 helper added on the 0.4.0 development tree.

AlmaLinux 9.9 still defaults to Python 3.9, and its AppStream FreeRDP is
2.11.7, while v-EMU needs Python 3.11+ and FreeRDP >= 3.26 for seamless mode.
The `setup.sh` and `.run` in the main tree intentionally target Fedora 45 and
RHEL-family 10.3, so EL9 uses a contrib wrapper instead.

## Install

```bash
git clone https://github.com/withcoresystem/v-EMU vEMU
cd vEMU

# Read-only checks first
./contrib/setup-el9.sh --precheck

# Core v-EMU plus Image Builder packages
./contrib/setup-el9.sh

# Also build FreeRDP 3.33.1 into /usr/local for seamless mode
./contrib/setup-el9.sh --with-freerdp
```

`contrib/setup-el9.sh`:

- installs Python 3.11 when only Python 3.9 is present;
- enables CRB and installs `libvirt-devel` and `python3.11-devel`;
- installs `libvirt-python` for the system Python used by `vemud`;
- installs `image-builder`, `osbuild` and `osbuild-depsolve-dnf`;
- runs the main `setup.sh` with the EL9 Python; and
- optionally runs `contrib/setup-freerdp-el9.sh`.

It does **not** modify `setup.sh`, `setup.run` or the released 0.3.1 tree.

After the first install, log out/in or reboot for group and socket changes.

## FreeRDP from source

AlmaLinux 9.9 ships FreeRDP 2.11.7, below the RAIL minimum. Build 3.33.1 into
`/usr/local` (the distribution package stays untouched):

```bash
./contrib/setup-freerdp-el9.sh --precheck
./contrib/setup-freerdp-el9.sh
```

The build uses X11 (Xwayland is enough on a Wayland desktop), without audio,
printing, smartcard, USB redirection or Kerberos. The distribution's
`/usr/bin/xfreerdp` remains; `/usr/local/bin` normally wins in PATH. If it
does not, set `VEMU_XFREERDP=/usr/local/bin/xfreerdp`.

Check:

```bash
xfreerdp /version          # FreeRDP 3.33.1
vemu seamless status DOMAIN
```

## Image Builder

The EL9 helper installs the Image Builder tools. On Alma 9.9, `image-builder`
79 can target `almalinux-10.3` qcow2 and the repository blueprint
`templates/alma-10.3-gnome/blueprint-qemu.toml` builds successfully (about
6-7 minutes, 922 packages, ~1.6 GiB output).

Register an `images` storage role before building so v-EMU can apply the
`virt_image_t` label to the output directory.

## Windows seamless

A Windows template built on Fedora 45/QEMU 11.1 may need adjustment on an
EL9/QEMU 10.1 host:

- machine type: use a machine supported by the host QEMU
  (`pc-q35-rhel9.8.0` on Alma 9.9; `pc-q35-11.1` is not available);
- disk bus: the RHEL 9 QEMU build has no NVMe device, so use SATA (`sda`) or
  ensure the guest has the matching driver.

The tested flow used the `W11-26H2-base` template, a sysprepped Windows 11
image with Secure Boot and TPM, a `lab-nat` network, and the ProUser account.
`vemu seamless prepare --username ProUser --ip <guest-ip>` completed, and the
Notepad RAIL test passed with FreeRDP 3.33.1:

```text
Notepad: connected and stayed open (5.0s)
```

This is a lab validation, not a guarantee for every Windows template. Test
RAIL on the exact template and desktop you plan to use.

## Limits

- Alma 9.9 is late in its lifecycle; treat it as best-effort, not a primary
  target.
- The EL9 FreeRDP build is minimal (no audio/printing/smartcard/USB/Kerberos).
  Add the corresponding `-DWITH_*` options and packages if needed.
- Image Builder is supported on the host, but guest templates still need the
  usual v-EMU packaging and testing.
- The GUI opens on Alma 9.9; it was also reported to open under WSL3, but that
  is not a supported host tier and KVM/GPU behaviour there is not validated.
- Experimental 3D remains out of scope for the EL9 tier.

See [`WINDOWS-SEAMLESS.md`](WINDOWS-SEAMLESS.md) for seamless details. The
full internal test record is kept with the maintainer.
