"""SQLite cache and package/group lookup for Image Builder.

The cache is deliberately advisory. ``image-builder`` and the osbuild solver
remain the source of truth; a missing or corrupt cache must never block a
build. Catalog data changes with the host engine, package metadata is refreshed
weekly, and package groups are cached longer because their membership changes
rarely.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlparse

from .errors import ValidationError

CATALOG_TTL = 90 * 24 * 60 * 60
REPO_TTL = 90 * 24 * 60 * 60
PACKAGE_TTL = 7 * 24 * 60 * 60
GROUP_TTL = 90 * 24 * 60 * 60

SOLVER_PATHS = (
    "/usr/libexec/osbuild-depsolve-dnf",
    "/usr/lib/osbuild/osbuild-depsolve-dnf",
)

SCHEMA_VERSION = 1


def cache_root() -> Path:
    configured = os.environ.get("VEMU_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        return Path(xdg).expanduser()
    return Path.home() / ".cache"


def default_db_path() -> Path:
    return cache_root() / "vemu" / "metadata.sqlite3"


def solver_cache_dir(target: str) -> Path:
    return cache_root() / "vemu" / "solver" / target.replace("/", "-")


def host_cache_id() -> str:
    """Stable host identifier for cache rows."""
    for candidate in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = Path(candidate).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    return platform.node() or "localhost"


def target_key(
    distro: str, arch: str, extra_repos: list[str] | tuple[str, ...] = ()
) -> str:
    """Cache key for one distro/arch/repo set."""
    digest = hashlib.sha256(
        "\n".join(sorted(str(url) for url in extra_repos)).encode("utf-8")
    ).hexdigest()[:12]
    return f"{distro}/{arch}/{digest}"


def releasever_for(distro: str) -> str:
    """Extract the release from image-builder distro names."""
    name = distro.strip()
    if "-" not in name:
        raise ValidationError(
            f"Cannot determine the release for distro '{distro}'."
        )
    return name.rsplit("-", 1)[-1]


def lookup_image_type(distro: str) -> str:
    """Pick an image type only to obtain repository definitions."""
    name = distro.lower()
    if name.startswith("fedora"):
        return "generic-qcow2"
    if any(
        family in name
        for family in ("rhel", "almalinux", "rocky", "centos", "oracle")
    ):
        return "qcow2"
    raise ValidationError(
        f"No Image Builder lookup type for distro '{distro}'."
    )


def solver_path() -> str | None:
    configured = os.environ.get("VEMU_OSBUILD_DEPSOLVE")
    if configured:
        return configured
    for candidate in SOLVER_PATHS:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("osbuild-depsolve-dnf")


class ImageMetadataStore:
    """Small SQLite store shared by the catalog and package lookup."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else default_db_path()

    # ------------------------------------------------------------- lifecycle

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(str(self.path), timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=5000")
            self._ensure_schema(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS catalog (
                host_id TEXT NOT NULL,
                engine_version TEXT NOT NULL,
                distro TEXT NOT NULL,
                arch TEXT NOT NULL,
                image_type TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (host_id, engine_version, distro, arch, image_type)
            );
            CREATE TABLE IF NOT EXISTS cache (
                kind TEXT NOT NULL,
                target TEXT NOT NULL,
                query TEXT NOT NULL,
                payload TEXT NOT NULL,
                fetched_at REAL NOT NULL,
                PRIMARY KEY (kind, target, query)
            );
            """
        )
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT OR IGNORE INTO meta(key, value) "
                "VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        elif str(row["value"]) != str(SCHEMA_VERSION):
            raise ValidationError(
                "Image metadata cache schema changed; delete the cache file "
                f"at {self.path} and retry."
            )

    def close(self) -> None:
        """Compatibility hook; connections are opened per operation."""
        return None

    # --------------------------------------------------------------- catalog

    def catalog_get(
        self,
        host_id: str,
        engine_version: str,
        *,
        ttl: int = CATALOG_TTL,
    ) -> list[dict[str, str]] | None:
        return self._catalog(host_id, engine_version, ttl=ttl)

    def catalog_get_stale(
        self, host_id: str, engine_version: str
    ) -> list[dict[str, str]] | None:
        return self._catalog(host_id, engine_version, ttl=None)

    def _catalog(
        self, host_id: str, engine_version: str, *, ttl: int | None
    ) -> list[dict[str, str]] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT MAX(fetched_at) AS fetched_at FROM catalog "
                "WHERE host_id = ? AND engine_version = ?",
                (host_id, engine_version),
            ).fetchone()
            if row is None or row["fetched_at"] is None:
                return None
            if ttl is not None and time.time() - float(row["fetched_at"]) > ttl:
                return None
            rows = connection.execute(
                "SELECT distro, arch, image_type FROM catalog "
                "WHERE host_id = ? AND engine_version = ? "
                "ORDER BY distro, image_type, arch",
                (host_id, engine_version),
            ).fetchall()
        return [dict(item) for item in rows]

    def catalog_put(
        self,
        host_id: str,
        engine_version: str,
        entries: list[dict[str, str]],
    ) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM catalog WHERE host_id = ? AND engine_version = ?",
                (host_id, engine_version),
            )
            connection.executemany(
                "INSERT INTO catalog"
                "(host_id, engine_version, distro, arch, image_type, "
                " fetched_at) VALUES(?, ?, ?, ?, ?, ?)",
                [
                    (
                        host_id,
                        engine_version,
                        str(item.get("distro", "")),
                        str(item.get("arch", "")),
                        str(item.get("image_type", "")),
                        now,
                    )
                    for item in entries
                ],
            )

    # ----------------------------------------------------------- generic cache

    def cache_get(
        self,
        kind: str,
        target: str,
        query: str = "",
        *,
        ttl: int | None = None,
    ) -> tuple[Any, float] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload, fetched_at FROM cache "
                "WHERE kind = ? AND target = ? AND query = ?",
                (kind, target, query),
            ).fetchone()
        if row is None:
            return None
        fetched_at = float(row["fetched_at"])
        if ttl is not None and time.time() - fetched_at > ttl:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            return None
        return payload, fetched_at

    def cache_put(
        self,
        kind: str,
        target: str,
        query: str,
        payload: Any,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO cache"
                "(kind, target, query, payload, fetched_at) "
                "VALUES(?, ?, ?, ?, ?)",
                (
                    kind,
                    target,
                    query,
                    json.dumps(payload, ensure_ascii=False),
                    time.time(),
                ),
            )

    def clear_target(self, target: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM cache WHERE target = ?", (target,)
            )

    def clear_all(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM catalog")
            connection.execute("DELETE FROM cache")

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            catalog = connection.execute(
                "SELECT COUNT(*) AS count FROM catalog"
            ).fetchone()["count"]
            cache = connection.execute(
                "SELECT COUNT(*) AS count FROM cache"
            ).fetchone()["count"]
        return {"catalog": int(catalog), "cache": int(cache)}


def _tail(text: str, limit: int = 500) -> str:
    value = (text or "").strip()
    return value[-limit:] if len(value) > limit else value


def search_patterns(query: str, *, max_terms: int = 32) -> list[str]:
    """Convert a comma-separated query into solver glob patterns."""
    terms = [part.strip() for part in query.split(",") if part.strip()]
    return [
        term if "*" in term else f"*{term}*"
        for term in terms[:max_terms]
    ]


def _repo_label(url: str) -> str:
    parsed = urlparse(url)
    repo = parse_qs(parsed.query).get("repo")
    if repo:
        return f"{parsed.netloc}/{repo[0]}"
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) >= 3:
        return f"{parsed.netloc}/{parts[-3]}"
    if parts:
        return f"{parsed.netloc}/{parts[-1]}"
    return parsed.netloc or url


def repos_from_manifest(
    distro: str,
    arch: str,
    *,
    extra_repos: list[str] | tuple[str, ...] = (),
    executable: str | None = None,
    timeout: float = 180.0,
) -> list[dict[str, Any]]:
    """Resolve target repository definitions through image-builder itself.

    ``image-builder`` embeds the distro repository configuration. Generating a
    minimal manifest is the supported way to obtain the same URLs for a
    read-only lookup, instead of duplicating vendor repo maps in v-EMU.
    """
    tool = executable or shutil.which("image-builder")
    if not tool:
        raise ValidationError(
            "image-builder was not found; package lookup needs the same tool "
            "as Image Builder."
        )
    image_type = lookup_image_type(distro)
    with tempfile.TemporaryDirectory(prefix="vemu-lookup-") as tmp:
        blueprint = Path(tmp) / "lookup.toml"
        blueprint.write_text(
            f'name = "vemu-lookup"\ndistro = "{distro}"\n',
            encoding="utf-8",
        )
        command = [
            tool,
            "manifest",
            image_type,
            "--distro",
            distro,
            "--arch",
            arch,
            "--blueprint",
            str(blueprint),
        ]
        for url in extra_repos:
            command.extend(["--extra-repo", str(url)])
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError(
                f"Cannot prepare repository metadata: {exc}"
            ) from exc
    if result.returncode != 0:
        raise ValidationError(
            "Cannot prepare repository metadata: "
            + (_tail(result.stderr or result.stdout) or "manifest failed")
        )
    try:
        payload = json.loads(result.stdout)
        mirrors = payload["sources"]["org.osbuild.librepo"]["options"][
            "mirrors"
        ]
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "image-builder returned an unexpected manifest for repository "
            "lookup."
        ) from exc
    repos: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(mirrors.values()):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        repo: dict[str, Any] = {
            "id": f"lookup-{index}",
            "name": _repo_label(url),
            "gpgcheck": False,
            "repo_gpgcheck": False,
        }
        kind = str(item.get("type") or "baseurl")
        if kind == "metalink":
            repo["metalink"] = url
        elif kind == "mirrorlist":
            repo["mirrorlist"] = url
        else:
            repo["baseurl"] = [url]
        repos.append(repo)
    for url in extra_repos:
        value = str(url or "")
        if value and value not in seen:
            seen.add(value)
            repos.append(
                {
                    "id": f"lookup-extra-{len(repos)}",
                    "name": _repo_label(value),
                    "gpgcheck": False,
                    "repo_gpgcheck": False,
                    "baseurl": [value],
                }
            )
    if not repos:
        raise ValidationError(
            "image-builder did not report any repositories for this target."
        )
    return repos


def repos_for_target(
    store: ImageMetadataStore,
    distro: str,
    arch: str,
    *,
    extra_repos: list[str] | tuple[str, ...] = (),
    refresh: bool = False,
) -> tuple[str, list[dict[str, Any]]]:
    key = target_key(distro, arch, extra_repos)
    if not refresh:
        cached = store.cache_get("repos", key, "", ttl=REPO_TTL)
        if cached is not None:
            return key, list(cached[0])
    repos = repos_from_manifest(
        distro, arch, extra_repos=extra_repos
    )
    store.cache_put("repos", key, "", repos)
    return key, repos


class SolverLookup:
    """Read-only package and group queries through the osbuild solver."""

    def __init__(
        self,
        distro: str,
        arch: str,
        repos: list[dict[str, Any]],
        *,
        store: ImageMetadataStore | None = None,
        target: str | None = None,
        cache_root_dir: str | Path | None = None,
        solver: str | None = None,
    ) -> None:
        self.distro = distro
        self.arch = arch
        self.repos = repos
        self.store = store if store is not None else ImageMetadataStore()
        self.target = target or target_key(distro, arch)
        self.releasever = releasever_for(distro)
        if cache_root_dir:
            root = Path(cache_root_dir).expanduser()
            self.cache_dir = root / self.target.replace("/", "-")
        else:
            self.cache_dir = solver_cache_dir(self.target)
        self.solver = solver or solver_path()

    def clear_cache(self) -> None:
        """Drop solver metadata; repository cache rows are cleared separately."""
        shutil.rmtree(self.cache_dir, ignore_errors=True)

    # ---------------------------------------------------------------- solver

    def _request(self, payload: dict[str, Any]) -> Any:
        if not self.solver:
            raise ValidationError(
                "osbuild-depsolve-dnf was not found; package lookup is "
                "unavailable on this host."
            )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        request = {
            "arch": self.arch,
            "releasever": self.releasever,
            "cachedir": str(self.cache_dir),
            **payload,
        }
        try:
            result = subprocess.run(
                [self.solver],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=180,
                env={**os.environ, "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError(
                f"Package lookup failed: {exc}"
            ) from exc
        if result.returncode != 0:
            detail = _tail(result.stderr or result.stdout)
            raise ValidationError(
                "Package lookup failed: "
                + (detail or "the osbuild solver returned an error")
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                "Package lookup returned an unexpected response."
            ) from exc
        if isinstance(payload, dict) and payload.get("kind"):
            raise ValidationError(
                "Package lookup failed: "
                + str(payload.get("reason") or payload["kind"])
            )
        return payload

    def search_packages(
        self, query: str, *, latest: bool = True, limit: int = 200
    ) -> list[dict[str, str]]:
        patterns = search_patterns(query)
        if not patterns:
            return []
        cache_key = (
            "search:" + "|".join(patterns) + f":{int(latest)}"
        )
        cached = self.store.cache_get(
            "packages", self.target, cache_key, ttl=PACKAGE_TTL
        )
        if cached is not None:
            return list(cached[0])[:limit]
        payload = self._request(
            {
                "command": "search",
                "arguments": {
                    "repos": self.repos,
                    "search": {
                        "packages": patterns,
                        "latest": latest,
                    },
                },
            }
        )
        repo_names = {
            str(repo.get("id") or ""): str(
                repo.get("name") or repo.get("id") or ""
            )
            for repo in self.repos
        }
        rows: list[dict[str, str]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for item in payload if isinstance(payload, list) else []:
            if not isinstance(item, dict):
                continue
            epoch = int(item.get("epoch") or 0)
            version = str(item.get("version") or "")
            release = str(item.get("release") or "")
            display = f"{version}-{release}" if release else version
            if epoch:
                display = f"{epoch}:{display}"
            repo_id = str(item.get("repo_id") or "")
            name = str(item.get("name") or "")
            arch = str(item.get("arch") or "")
            dedupe_key = (name, display, arch, repo_id)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            rows.append(
                {
                    "name": name,
                    "version": display,
                    "arch": arch,
                    "repo": repo_names.get(repo_id, repo_id),
                    "summary": str(item.get("summary") or ""),
                    "description": str(item.get("description") or ""),
                    "license": str(item.get("license") or ""),
                    "url": str(item.get("url") or ""),
                }
            )
        rows.sort(key=lambda entry: (entry["name"], entry["arch"]))
        self.store.cache_put("packages", self.target, cache_key, rows)
        return rows[:limit]

    # ----------------------------------------------------------------- groups

    def _repo_files(self, directory: Path) -> None:
        for repo in self.repos:
            repo_id = str(repo.get("id") or "lookup")
            lines = [
                f"[{repo_id}]",
                f"name={repo.get('name') or repo_id}",
                "enabled=1",
                "gpgcheck=0",
                "repo_gpgcheck=0",
            ]
            for field in ("baseurl", "metalink", "mirrorlist"):
                value = repo.get(field)
                if field == "baseurl" and isinstance(value, list):
                    lines.extend(f"baseurl={url}" for url in value if url)
                elif value:
                    lines.append(f"{field}={value}")
            (directory / f"{repo_id}.repo").write_text(
                "\n".join(lines) + "\n", encoding="utf-8"
            )

    def group_list(
        self, *, include_hidden: bool = False
    ) -> list[dict[str, str]]:
        cache_key = "hidden" if include_hidden else ""
        cached = self.store.cache_get(
            "groups", self.target, cache_key, ttl=GROUP_TTL
        )
        if cached is not None:
            return list(cached[0])
        dnf = shutil.which("dnf")
        if not dnf:
            raise ValidationError(
                "dnf was not found; package groups are unavailable."
            )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="vemu-repos-") as tmp:
            repo_dir = Path(tmp)
            self._repo_files(repo_dir)
            command = [
                dnf,
                "-q",
                "--releasever",
                self.releasever,
                f"--setopt=reposdir={repo_dir}",
                f"--setopt=cachedir={self.cache_dir}",
                "--setopt=keepcache=True",
                "group",
                "list",
            ]
            if include_hidden:
                command.append("--hidden")
            try:
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    env={**os.environ, "LC_ALL": "C"},
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValidationError(
                    f"Cannot list package groups: {exc}"
                ) from exc
        if result.returncode != 0:
            raise ValidationError(
                "Cannot list package groups: "
                + (_tail(result.stderr or result.stdout) or "dnf failed")
            )
        groups: list[dict[str, str]] = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[0] == "ID":
                continue
            if parts[-1] not in {"yes", "no"}:
                continue
            groups.append(
                {
                    "id": parts[0],
                    "name": " ".join(parts[1:-1]),
                    "installed": parts[-1],
                }
            )
        groups.sort(key=lambda item: (item["name"].lower(), item["id"]))
        self.store.cache_put("groups", self.target, cache_key, groups)
        return groups

    def validate_group(self, group: str) -> dict[str, Any]:
        name = group.strip().lstrip("@")
        if not name:
            raise ValidationError("Enter a package group name.")
        cached = self.store.cache_get(
            "group", self.target, name, ttl=GROUP_TTL
        )
        if cached is not None:
            return dict(cached[0])
        payload = self._request(
            {
                "command": "depsolve",
                "arguments": {
                    "repos": self.repos,
                    "transactions": [
                        {
                            "package-specs": [f"@{name}"],
                            "install_weak_deps": False,
                        }
                    ],
                },
            }
        )
        packages = sorted(
            str(item.get("name") or "")
            for item in payload.get("packages", [])
            if isinstance(item, dict)
        )
        result = {
            "group": name,
            "count": len(packages),
            "packages": packages[:500],
            "truncated": len(packages) > 500,
        }
        self.store.cache_put("group", self.target, name, result)
        return result


__all__ = [
    "CATALOG_TTL",
    "GROUP_TTL",
    "PACKAGE_TTL",
    "ImageMetadataStore",
    "SolverLookup",
    "cache_root",
    "default_db_path",
    "host_cache_id",
    "lookup_image_type",
    "releasever_for",
    "repos_for_target",
    "repos_from_manifest",
    "search_patterns",
    "solver_cache_dir",
    "solver_path",
    "target_key",
]
