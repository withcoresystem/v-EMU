# First-boot payload examples

`post-setup.sh` is a test payload for the Image Builder first-boot TUI. It
installs **bat** and **btop**, removes a conservative list of optional desktop
packages when present, and writes:

```text
/var/log/vemu-post-setup-test.log
/var/lib/vemu/post-setup-test.done
```

It uses `dnf`/`rpm` only. Image Builder supports Fedora/RHEL-family hosts;
Debian, Ubuntu and openSUSE are not supported, so this example does not carry
`apt`/`zypper` branches. It is non-interactive and safe to run again. The
removal list only includes optional GNOME/KDE extras; it is filtered by the
package database, so missing packages are ignored.

## USB

```text
VEMU-FBOOT/
  vemu-firstboot/
    post-setup.sh
```

In the ISO wizard Firstboot step, enable the TUI, set **Script name** to
`post-setup.sh`, leave **Script URL** empty, and keep USB enabled. At first boot
the TUI finds the only `VEMU-FBOOT` device, runs that file and restarts after a
short countdown.

## HTTP(S)

Serve the directory:

```bash
python3 -m http.server 8000 --bind 0.0.0.0
```

Set **Script name** to `post-setup.sh` and **Script URL** to the base URL, for
example `http://10.0.2.2:8000/` in a QEMU user-network test, or
`https://deploy.example/firstboot/` in production. The TUI fetches
`<base>/<name>` automatically. A direct file URL also works when **Script name**
is empty.

Both fields empty keeps the manual menu. On failure the TUI returns to the menu
and keeps the log.
