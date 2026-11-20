# Windows seamless soak test

A repeatable mixed-workload soak for a **prepared seamless VM**. It simulates a
long desktop session rather than a benchmark: Excel recalculation, Word
copy/paste, browser animation, print jobs, clipboard round-trips, and file
saves to a VirtioFS share.

This is a lab helper, not a supported v-EMU feature. Run it on a disposable
test VM and test data only.

## Requirements

- A Windows 10/11 Pro/Enterprise guest already prepared with
  `vemu seamless prepare`.
- A logged-on desktop/RAIL session. Launch one published app first, for
  example `vemu seamless launch-app DOMAIN Excel`.
- Microsoft Excel and Word installed. Edge is used for the web workload.
- A writable VirtioFS share mapped to `Z:` (or pass `--share-drive`).
- A real print queue named `CUPS PDF` (or pass `--printer`). If the printer is
  missing, the workload still runs but `Out-Printer` errors are logged and
  print jobs are not validated.
- OpenSSH Server and the seamless SSH key/known-hosts state.
- About 2 GB free on `C:` plus free space on the share.

`run_soak.py` uses the saved seamless state (`host`, `username`, `key_path`,
`known_hosts`). It never stores or asks for a Windows password.

## Run from the host

```bash
# Start a RAIL session first; the workload must run in the logged-on session.
vemu seamless launch-app W11-Seamless Excel

# Default: 18 minutes, 20,000 Excel rows, CUPS PDF, Z:
.venv/bin/python contrib/windows/soak/run_soak.py W11-Seamless

# Short smoke run
.venv/bin/python contrib/windows/soak/run_soak.py W11-Seamless --minutes 1

# Different printer/share/rows
.venv/bin/python contrib/windows/soak/run_soak.py W11-Seamless \
  --minutes 18 --rows 20000 --printer "CUPS PDF" --share-drive Z:
```

The runner:

1. reads the seamless state and connects over SSH;
2. checks that an interactive Windows session exists and the share drive is
   accessible;
3. creates `C:\vemu\soak`, grants the Windows user write access, and uploads
   `soak.ps1`, `run-soak.cmd` and `stress.html`;
4. starts a scheduled task in the logged-on session;
5. waits for `soak.done` and prints `latest-summary.json`.

`Ctrl-C` stops waiting; the guest task keeps running.

## Run manually inside Windows

Copy this directory to `C:\vemu\soak` and grant the logged-on user write
access (the host runner does this automatically):

```powershell
icacls C:\vemu\soak /grant "$env:USERNAME:(OI)(CI)F"
C:\vemu\soak\run-soak.cmd -Minutes 18
```

The wrapper writes the PowerShell transcript to
`C:\vemu\soak\soak-task-<random>.log`. The authoritative progress log is on the
share.

## Workload

Each run creates a unique directory `Z:\soak-<stamp>` and records
`soak.log` plus a `summary.json` there and under `C:\vemu\soak\<stamp>`.

- Excel: 20,000 rows × 8 columns, formulas, then 200 random cell updates and a
  recalculation per iteration.
- Word: the Excel range is copied and pasted into one growing document,
  followed by a paragraph and save; this exercises Office clipboard/paste.
- Clipboard: a unique string is set and read back every iteration.
- Browser: Edge opens `stress.html` (canvas animation + periodic fetches).
- Print: every third iteration sends a text job to the configured printer.
- Share: `.xlsx` and `.docx` files stay on the VirtioFS drive; the final
  `summary.json` records iterations, elapsed minutes, print jobs, Word pages,
  process counts and free space.

`summary.json` example fields:

```json
{
  "minutes": 18,
  "iterations": 27,
  "print_jobs": 9,
  "elapsed_min": 18.3,
  "excel_rows": 20000,
  "word_pages": 91,
  "edge_processes": 9,
  "excel_processes": 3,
  "word_processes": 1,
  "free_c": 47719153664
}
```

## Interpreting results

- `RETRY word ... RPC_E_CALL_REJECTED` is Word COM being busy. The script
  retries up to six times; occasional retries are expected. `ERR word paste
  ... gave up` is a finding.
- A failing iteration is logged and the run continues. Check `soak.log` for
  repeated errors, not just the final summary.
- Verify the print path independently. On the CUPS PDF server, jobs appear as
  `/var/spool/cups-pdf/ANONYMOUS/document-job_*.pdf`.
- Keep `vemu seamless status DOMAIN` running or inspect `xfreerdp`/supervisor
  processes to confirm the RAIL session stays connected.
- Check CPU/RSS and guest Event Viewer if the run is slow or hangs.

## Reference run (2026-11-19)

`W11-Seamless` on Fedora 45, FreeRDP 3.33.1, AlmaLinux/Host QEMU 11.1:

- 18.3 minutes, 27 iterations;
- 20,000-row Excel workbook, 91 Word pages;
- 9 print jobs, verified as PDFs on `DEV-Fedora-45`;
- Edge animation, clipboard round-trips and xlsx/docx saves to the VirtioFS
  `Downloads` share;
- occasional Word `RPC_E_CALL_REJECTED` retries, no lost iteration;
- RAIL supervisor stayed connected for the full run.

See [`docs/WINDOWS-SEAMLESS.md`](../../../docs/WINDOWS-SEAMLESS.md) and
[`docs/design/windows-seamless-overview.md`](../../../docs/design/windows-seamless-overview.md)
for the current support and limitation notes.

## Limitations

- Office COM automation can be sensitive to first-run dialogs, activation and
  pending updates. Keep the guest logged in and avoid Windows Update during a
  run.
- PowerPoint is not part of the default workload; its COM export path was
  unreliable in testing. Add a separate PowerPoint step only if the guest has
  a proven setup.
- The script does not click UI controls, test UAC prompts, DPI scaling,
  multi-monitor layout, audio, USB or tray integration.
- A missing printer or `Z:` share changes what the run proves. Check the
  wrapper log and `soak.log` before drawing conclusions.
- Not a performance benchmark; results are machine- and workload-dependent.
