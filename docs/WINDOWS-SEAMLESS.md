# Windows seamless mode — user guide (0.3.0 preview)

Seamless mode publishes selected programs from a Windows VM as ordinary Linux
desktop windows over one FreeRDP RAIL connection. v-EMU prepares the Windows
guest, keeps one supervisor connection and creates Linux shortcuts; Windows
itself continues to own licensing, updates, UAC and application installs.

This guide covers the Windows-only **Seamless Mode** menu in the v-EMU UI and
the equivalent CLI commands. It is a preview for 0.3.0 development.

## 1. Before you start

| Requirement | Notes |
| --- | --- |
| Windows edition | Windows 11 Pro, Enterprise, Education or Pro for Workstations. Windows Home cannot host RDP. |
| Windows user | A local account with a password. A standard user is supported; log in once as that user so the Windows profile exists. |
| Guest agent | QEMU Guest Agent (`QEMU-GA`) installed and running. v-EMU uses it for setup and verification. |
| Display | QXL or VGA. Change the VM XML to QXL/VGA and restart Windows before activation. There is no Set QXL button in the wizard. |
| SSH | OpenSSH Server is preferred in the template. If absent, the helper enables the Windows capability; this can require Windows Update and a reboot. |
| Network | A stable address or DHCP reservation is recommended. NAT is fine for one host; bridged networking is fine when the host can reach the guest. |
| Host | v-EMU daemon running, a desktop session (KDE Wayland is the tested setup), and a compatible FreeRDP client. FreeRDP **>= 3.26.0** is required; **3.33.1** is recommended. Fedora/RHEL-family packages and openSUSE Leap 16.1 work; Debian 13 ships 3.15, which is unsupported for RAIL, so use the Flatpak 3.33.1 fallback. |
| Keyring | Optional Python `keyring` support. Without it, v-EMU asks for the password again when a launch needs it. |
| Storage | The VM must use a v-EMU-managed qcow2 chain if you want the pre-seamless checkpoint. An unmanaged VM can use `--no-checkpoint`, but then Restore is unavailable. |

### FreeRDP client support

Seamless mode needs a client with working RemoteApp/RAIL support. The tested
matrix is:

| Host | FreeRDP | Result |
| --- | --- | --- |
| Fedora 45 | 3.33.1 | Full validation |
| AlmaLinux 10.3 | 3.32.2 (AppStream) | RAIL core and input validated on GNOME Shell 49.5; ensure Xwayland is running after a fresh login |
| openSUSE Leap 16.1 | 3.26.0 (repo-oss) | RAIL core and input validated; 3.33.1 recommended for icon/resize polish |
| Debian 13 | 3.15 (distro) | **Not supported**: RAIL can stall at the Windows welcome screen |

`xfreerdp` and `xfreerdp3` are both accepted. For Debian 13, install the
Flatpak FreeRDP 3.33.1 client and point `VEMU_XFREERDP` at a small wrapper.
The Flatpak is an isolated fallback: it may block smartcard, USB redirection,
OS keyring, filesystem paths or advanced clipboard formats that the distro
package can access. Do not treat it as feature-equivalent to a system package.
If your environment depends on those features, use a distribution with a
current FreeRDP or build one from source.

A minimal direct-launch fallback on Debian 13:

```bash
flatpak install --user flathub com.freerdp.FreeRDP
cat > ~/.local/bin/xfreerdp-flatpak <<'EOF'
#!/bin/sh
exec flatpak run --command=xfreerdp com.freerdp.FreeRDP "$@"
EOF
chmod +x ~/.local/bin/xfreerdp-flatpak
VEMU_XFREERDP=~/.local/bin/xfreerdp-flatpak \
  vemu --direct seamless launch-app DOMAIN APP
```

Use `--direct` so the user-session environment is visible. Do not assume the
root daemon or generated shortcuts inherit `VEMU_XFREERDP`; for persistent use,
configure the daemon environment or install a current distro client.


After cloning a Windows VM, boot it once and install/update QGA, then reboot.
After a Windows feature update, re-run **Precheck** and expect a reboot.

Only one Windows VM can host the active RAIL supervisor at a time. Starting
seamless mode on another Windows VM pauses the previous supervisor; the
previous Windows session and its apps stay alive for reconnection.

## 2. What the helper does

Activation with the default checkpoint performs this sequence:

1. **Precheck** (button in the wizard): verifies the VM is running, the video
   model is QXL/VGA, QGA is reachable and Windows/guest preflight passes.
   Precheck does **not** validate the Windows password; the password is stored
   in the keyring for later RDP launches.
2. **Checkpoint:** gracefully shuts the guest down, saves the domain XML beside
   the seamless state, creates a v-EMU snapshot labelled
   `seamless-prepare`, and starts the guest again.
3. **Guest preparation:** applies the selected optimization profile
   (`Safe` by default), disables Edge/Chrome hardware acceleration, enables the
   Windows services used by v-EMU, installs a pinned SSH key, disables
   password/keyboard-interactive SSH login, enables RDP with NLA/TLS, pins the
   RDP certificate, and writes the RemoteApp allowlist.
4. **Desktop integration:** records state under
   `~/.local/state/vemu/seamless/` or `/var/lib/vemu/seamless/`, then creates
   Linux shortcuts and per-app icons for published programs.

The helper does not install Windows applications, bypass UAC, activate a
Windows licence, or change the VM's video model.

The checkpoint is the guest state at activation time. Applications and files
created after that point are lost if you choose **Disable seamless mode… →
Remove and restore**.

## 3. Activate seamless mode

1. Start the Windows VM and wait until the desktop is usable.
2. Open the VM context menu → **Seamless Mode** → **Activate seamless mode…**.
3. Enter the Windows user and password.
4. Choose the optimization profile:
   - `Safe` — recommended default.
   - `Balanced` — fewer background reductions.
   - `3D` — only for deliberately graphics-heavy guests.
5. Click **Precheck**. The dialog opens immediately and runs the check in the
   background; use the progress/status line. If it fails, fix the reported
   item (QXL/VGA, QGA, Windows login, SSH/firewall, etc.) and run Precheck
   again.
6. When Precheck passes, **Activate** becomes available. Click it and wait;
   Windows may restart while the checkpoint is created. This can take a
   minute.
7. If Windows reports pending updates, reboot once inside Windows, then reopen
   the VM menu. The checkpoint is reused.
8. Open **Manage Windows programs…** to refresh the list and publish programs.

The activation password is only a convenience for RDP; the helper uses the
SSH key and does not write the password into VM XML, state or argv.

### CLI equivalent

```bash
vemu seamless prepare DOMAIN --username WindowsUser [--profile Safe]
```

The CLI requires the Windows user name; use the GUI if you want the helper to
save the RDP password in the keyring. Use `--no-checkpoint` only for an
unmanaged/legacy VM where you accept that Restore will not be available.

## 4. Publish and launch programs

**Manage Windows programs…** opens the published-program manager.

| Action | What it does |
| --- | --- |
| **Refresh** | Rescans Start-menu shortcuts, Program Files and, when enabled, Store/MSIX apps. |
| **Show built-in Windows tools** | Includes system tools under `C:\Windows` (off by default). |
| **Show Store/MSIX apps** | Includes packaged apps such as Calculator, Paint, Terminal and third-party MSIX apps (off by default). Names come from the Start menu; the package icon is used. |
| **Add…** | Publishes a selected program or a manual Windows path. |
| **Test launch…** | Opens one app over a temporary RAIL connection and marks it Verified if it stays connected. Verify the actual window/dialogs manually. |
| **Remove** | Removes the shortcut and RemoteApp allowlist entry. |
| **Forget password** | Deletes the saved RDP password from the OS keyring. |

Publishing a program creates a `.desktop` shortcut in the applications menu.
Launching any shortcut connects the single supervisor, then starts the app in
the existing Windows session through an interactive scheduled task. The first
launch may take longer while Windows starts; later launches reuse the session.

### CLI equivalents

```bash
vemu seamless apps DOMAIN [--include-system] [--include-store]
vemu seamless refresh-apps DOMAIN [--include-system] [--include-store]
vemu seamless add-app DOMAIN "Name" 'C:\Program Files\Vendor\app.exe' \
  [--arguments '...'] [--no-compat]
vemu seamless remove-app DOMAIN "Name"
vemu seamless test-app DOMAIN "Name"
vemu seamless launch-app DOMAIN "Name"
```

Store/MSIX apps are launched through the Windows activation API when the
packaged helper is available, with an Explorer shell fallback. A published
Store program still shows a technical `C:\Windows\explorer.exe` entry in the
state/allowlist, but the UI shows **Store app** and the display name.

## 5. Pause seamless mode

Use **Seamless Mode → Disable seamless mode…** and choose **Pause** when you
want to stop using seamless mode temporarily. The same dialog also offers
**Remove and restore**; see section 6.

- Stops the RAIL supervisor and seed.
- Removes published shortcuts and cached icons.
- Remembers the previous program list so Activate/Prepare can republish it
  without retyping; the list is not published while paused.
- Keeps RDP, SSH, guest optimization and the prepared Windows state.
- Leaves the guest running.
- Activating again reuses the existing setup and checkpoint; apps can be
  republished from Manage Windows programs.

CLI:

```bash
vemu seamless deactivate DOMAIN
```

Pause is not a rollback. It is the normal reversible “turn off the Linux side”
operation.

## 6. Remove seamless mode and restore the checkpoint

Use **Seamless Mode → Disable seamless mode…** and choose **Remove and restore
the pre-seamless checkpoint** only when you want to undo the guest changes.

This is destructive:

- stops the supervisor;
- shuts the guest down;
- restores the XML backup and reverts the v-EMU snapshot through the managed
  chain;
- removes published shortcuts, icons, seamless state, rail logs and generated
  SSH key/host-key files;
- leaves the guest **stopped**;
- loses Windows changes, apps and files created after the checkpoint.

The option is disabled when no checkpoint manifest exists; use Pause instead.
The dialog has a **Forget saved Windows password** checkbox, checked by default.

CLI:

```bash
vemu seamless rollback DOMAIN
vemu seamless restore DOMAIN        # same command
```

If the checkpoint layer was deleted or compacted outside v-EMU, the normal
**Remove and restore** action is unavailable. This is a recovery/development
case; the context menu does not expose a special action. Use the CLI:

```bash
vemu seamless rollback DOMAIN --host-only
```

Host-only cleanup removes Linux shortcuts/icons/state/SSH files and stops the
supervisor; the Windows guest and any checkpoint are kept.

### Checkpoint protection

- `vemu snapshot delete` refuses the seamless checkpoint layer unless
  `--force` is used.
- `vemu snapshot compact` refuses a seamlessly prepared VM unless `--confirm`
  is used.
- Both overrides invalidate the seamless rollback manifest, so a later restore
  reports a clear missing-checkpoint error instead of reverting to the wrong
  state.

### Deleting a VM

Deleting a VM does not require a rollback first. v-EMU stops the RAIL
supervisor and removes the published shortcuts/icons in both delete modes:

- **Delete VM only** keeps the chain, seamless state, XML backup and SSH files
  for recovery.
- **Delete VM and disks** also removes seamless state, keys and the saved
  keyring password.
- Experimental 3D host `qemu.conf` changes are **not** restored automatically;
  see [`WINDOWS-3D-PREVIEW.md`](WINDOWS-3D-PREVIEW.md).

If a VM was deleted outside v-EMU (`virsh undefine`, Cockpit), clean the host
side with:

```bash
vemu seamless rollback DOMAIN --host-only --forget-password
```

This works while the seamless state file still exists, even though the libvirt
domain is gone.

## 7. Everyday operation

```bash
vemu seamless status DOMAIN [--json]
vemu seamless stop DOMAIN
vemu seamless video DOMAIN --model qxl
```

- `status` shows the saved state, live preflight and checkpoint.
- `stop` stops the supervisor but leaves the Windows session/apps alive; the
  next shortcut reconnects.
- Shortcuts share one RAIL supervisor. Do not create a second FreeRDP
  connection for the same user; it takes over and disconnects the first.
- The supervisor stops itself a few seconds after the last RAIL window closes
  so a leftover pointer grab cannot trap host clicks. The Windows session is
  kept.

## 8. Core validation status

The current preview has been exercised on `W11-Seamless` with:

- bidirectional text clipboard between host and guest;
- an IPP printer on a Linux DEV VM backed by CUPS PDF;
- VirtioFS file sharing with the host;
- USB passthrough into the Windows guest;
- audio output (occasional slight stutter accepted);
- a 12-minute mixed soak: Excel, Word, PowerPoint, Edge animation, Visual
  Studio and repeated C# compilation. The RAIL supervisor stayed connected for
  the full run with no transport disconnect.
- an 18-minute mixed soak (2026-11-19): 27 iterations, a 20,000-row Excel
  workbook, 91 Word pages from repeated Excel→Word paste, 9 CUPS PDF print
  jobs (PDFs verified on `DEV-Fedora-45`), an Edge animation page, clipboard
  round-trips, and xlsx/docx saves to the VirtioFS Downloads share (`Z:`).
  Word RPC rejections were retried; the supervisor stayed connected.
  Reproduce it with [`contrib/windows/soak/README.md`](../contrib/windows/soak/README.md).

## 9. Known limitations

- **Resize:** FreeRDP 3.32–3.33 coalesces RAIL size updates, so drag-resize can
  end at an earlier, smaller size. A patched client improves this, but resize
  is still not native/full-console equivalent. See the overview and
  `windows-seamless-rdp.md`.
- **Tray and per-app task grouping:** tray forwarding is not implemented; all
  RAIL windows share `WM_CLASS=RAIL`. GNOME shortcuts set
  `StartupWMClass=RAIL` so the dock shows a (grouped) icon; KDE omits it to
  keep per-window icons. v-EMU also removes a stale `StartupWMClass=RAIL` from
  every v-EMU shortcut on KDE during activation/add-app/sync. Otherwise KDE can
  associate a RAIL window with another app's launcher, for example showing the
  Notepad++ icon for Excel. Regenerate shortcuts when switching desktops.
  GNOME has the same ambiguity: with several apps/domains carrying
  `StartupWMClass=RAIL`, the dock can pick one arbitrary launcher icon, and
  removing the line does not guarantee the window's own icon because every
  RAIL window still shares `WM_CLASS=RAIL`.
- **Keyboard layout:** FreeRDP forwards physical keycodes and the Windows guest
  applies its own layout. Changing the Linux input source (for example GNOME
  `de`) does not make Notepad type German; configure the Windows layout or use a
  client keyboard-layout option. Non-US layouts were not validated for 0.3.0.
- **Per-app RDP sessions / multi-session:** not planned as an icon-grouping
  workaround. TermsrvPatcher on the tested Windows 11 26H2 guest allowed
  concurrent sessions only for different Windows users, not for the same user;
  the second same-user connection disconnected the first. Different-user
  sessions also lose cross-app integration and duplicate profile, clipboard,
  audio and drive state. v-EMU keeps one shared RAIL session. See
  `windows-seamless-rdp.md`.
- **UAC:** appears as a secure desktop; the user must answer. Do not automate
  it.
- **Store/MSIX:** discovery is opt-in; package apps may have their own file
  pickers/UAC and some overlay tools need manual testing.
- **Clipboard:** bidirectional RDP text clipboard was verified host↔guest
  while connected; rich formats/file clipboard may vary.
- **Audio:** implemented; a slight occasional stutter is possible and was
  accepted for the current release. Tune `--audio-latency`/`--audio-quality`.
- **Multi-monitor:** optional `--multimon`, experimental; RAIL layout varies.
- **AVC444:** can reduce bandwidth for high-motion content but requires a
  Windows server policy and is not a resize fix.
- **One active Windows VM/session:** a second connection takes over. Shortcut
  launches show a desktop notification when another VM's RAIL supervisor is
  paused; the Test launch dialog reports it too.
- **No checkpoint** on an unmanaged VM or after a forced snapshot
  deletion/compaction; restore is unavailable.

## 10. Troubleshooting

| Symptom | Check |
| --- | --- |
| Precheck fails on QXL/VGA | Change the VM XML to QXL or VGA and restart Windows. |
| Precheck cannot reach QGA | Start the VM, log into Windows and check the QEMU Guest Agent service. |
| Activation says SSH key login not ready | Wait for Windows boot/sshd; check the detailed `Last error`. A per-attempt SSH timeout is retried (up to 90 attempts); the key must be mode 0600 and owned by the desktop user. |
| Activation says optimizer assets not found | Reinstall with `sudo ./setup.sh --standard` so `share/vemu/windows` is present; verify the daemon can read the installed data files. |
| Prepare failed and retry seems to add another checkpoint | The pre-prepare checkpoint is now reused: a failed prepare keeps one `seamless-prepare` layer and later retries attach to it instead of creating duplicates. Remove it with the normal rollback/restore flow only if you want to discard that rollback point. |
| Launch says FreeRDP is too old | Install FreeRDP >= 3.26.0. Debian 13's 3.15 is unsupported; use the Flatpak 3.33.1 fallback and read its sandbox caveats. |
| No RAIL window appears on GNOME after reboot | v-EMU uses xfreerdp through Xwayland. If GNOME has not started Xwayland yet, log out/in or launch a simple X11 client once; an xfreerdp process started before Xwayland can hang without a display. |
| Store app missing from the list | Enable **Show Store/MSIX apps** and Refresh. |
| Store app has an Explorer-style path | The state keeps the explorer launcher for RemoteApp; the UI shows **Store app**. Remove/re-add if the package icon is stale. |
| Pause removed my apps | Pause removes published shortcuts but keeps guest setup; re-add apps or activate/publish again. |
| Restore action missing | No checkpoint manifest exists (VM was never activated with a checkpoint, or it was deleted/compacted). Remove is disabled; use Pause, or the `--host-only` recovery command if host state must be cleared. |
| Restore fails after snapshot deletion | Use `vemu seamless rollback DOMAIN --host-only`; host state is kept until the guest revert succeeds. |
| Clicks stop after closing a RAIL app | The watchdog stops the supervisor after the last window. If using an older build, press Right Ctrl or `vemu seamless stop DOMAIN`. |
| Resize snaps back or ends smaller | Known FreeRDP issue; use a patched client and see the resize section. |
| Wrong app icon on KDE (for example Excel shows Notepad++) | A shortcut written while GNOME was active kept `StartupWMClass=RAIL`. Activate the domain or run `vemu seamless shortcuts DOMAIN`; v-EMU removes the stale class from all of its shortcuts on KDE. |
| UAC blocks the session | Answer it in Windows; v-EMU does not bypass secure desktop. |

## 11. Related documents

- [`../MANUAL.md`](../MANUAL.md) — install, daily use and host support.
- [`USER-GUIDE.md`](USER-GUIDE.md) — concise in-app guide.
