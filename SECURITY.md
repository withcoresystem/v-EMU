# Security policy

v-EMU is a local, lab-first desktop application. It manages libvirt/QEMU
domains on one host and runs a root daemon for privileged storage, ownership
and libvirt operations.

## Supported versions

Security fixes target the current `main` branch and the latest tagged release.
Older releases are not maintained.

## Reporting a vulnerability

Do **not** open a public issue for a vulnerability.

Use GitHub's **private vulnerability reporting** for the repository when it is
available. If that is unavailable, contact the maintainer through the GitHub
profile listed on the repository.

Please include:

- v-EMU version and host distribution;
- a short description of the impact;
- reproduction steps or proof of concept;
- relevant logs with secrets, IP addresses and VM data removed.

There is no bug bounty and no guaranteed response time. Reports are handled on
a best-effort basis.

## Scope

In scope:

- the `vemud` daemon, socket permissions and RPC validation;
- storage path policy, symlink handling and file deletion;
- CLI/UI operations that cross the daemon privilege boundary;
- command construction and privilege escalation in `setup.sh`;
- data exposure through logs, backups, baselines or share metadata.

Out of scope for this project:

- vulnerabilities in QEMU, KVM, libvirt, the kernel or distribution packages;
  report those upstream;
- guest operating-system vulnerabilities;
- attacks that already require root on the host;
- misconfiguration of SELinux policy outside v-EMU's documented
  storage repair;
- direct edits made with `virsh`, Cockpit or virt-manager that bypass v-EMU.

## Security model and boundaries

- The daemon runs as root. Client write paths are restricted to registered
  storage locations, but this is not a sandbox against root or other host tools.
- **External/read-only** prevents v-EMU from mutating a domain. It does not stop
  `virsh`, Cockpit or virt-manager from changing the same domain.
- Storage deletion is conservative. Only unreferenced files and orphan chains
  can be deleted through the UI.
- v-EMU does not manage host firewalling, SELinux policy for arbitrary
  paths, or enterprise subscription repositories.
- Lab defaults, including the documented Windows administrator password, are
  for disposable labs. Change them for any environment that matters.

## Runtime network access

v-EMU is local-only. There is no telemetry, analytics, update checker, remote
API or long-lived outbound connection. `vemud` does not open a TCP listener:
clients use a Unix socket, and VNC/noVNC plus the serial console bind to
`127.0.0.1`.

Internet access happens only while the user runs a specific action:

| Action | Destination | Trigger | Duration | Data |
|---|---|---|---|---|
| OSBuild catalog | none | Open dialog / Refresh | Local command | `image-builder list` only |
| Image Builder validate/preflight | Distro repositories (and declared extra repos) | Validate or Preflight | One `image-builder manifest` run | Repository metadata |
| Image Builder build | Distro and extra repositories; RHEL entitlement when applicable | Build | While the build job runs | Repository metadata and packages |
| Package/group lookup | Distro and extra repositories | Search, Load groups or Validate | One query, then cached | Repository metadata |
| Firstboot URL payload | URL chosen by the user | First boot inside the installed guest | One-shot with retries | Setup payload or script |
| Guest NAT | External network through the host | Guest traffic | Controlled by the guest | Guest data |
| SSH / console | Guest address or `127.0.0.1` | User opens SSH or console | While the session is open | Console or SSH traffic |
| `setup.sh` install/upgrade | Host distribution repositories | Install or upgrade | One-off | Host packages |

Notes:

- Image Builder and package lookup use the host proxy, DNS and RHEL
  subscription configuration. v-EMU does not store or proxy those credentials.
- Package lookup disables GPG checking for its read-only metadata queries, so
  results are advisory. A real build still runs the full manifest preflight and
  uses image-builder's repository policy.
- `--extra-repo` entries given to image-builder are **not GPG-checked** by the
  tool. Use only trusted repository URLs, and do not put credentials in a URL:
  it can appear in the process command line and build log.
- The SQLite cache is a local copy of repository metadata and query results. It
  contains no build secrets and is safe to delete. Package data expires after
  seven days; catalog, repository and group data after 90 days.
- The firstboot URL is fetched by the installed guest at first boot, not by the
  v-EMU host at ISO build time. USB payloads work offline.
- For air-gapped use, build with local repository overrides
  (`VEMU_IMAGE_BUILDER_REPO_DIR`/`--force-repo-dir`), use cached lookup data,
  choose USB firstboot payloads, and attach guests to an isolated network.
