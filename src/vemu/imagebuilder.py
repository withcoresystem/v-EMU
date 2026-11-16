"""Image Builder data adapters.

``FakeImageBuilderAdapter`` keeps the UI and tests runnable without
``image-builder``.  ``RealImageBuilderAdapter`` adds host/tool detection,
cancellable manifest-based package/repo preflight, build-command construction
and a privileged build job.  ``default_adapter()`` picks the real adapter when
the host is ready and falls back to the preview adapter otherwise.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from . import iso_builder
from .errors import ValidationError
from .image_metadata import CATALOG_TTL, ImageMetadataStore, host_cache_id
from .iso_builder import IsoOptions

BuildMode = Literal["qcow2", "iso"]

REQUIRED_TOOLS = (
    "image-builder",
    "osbuild",
    "osbuild-depsolve-dnf",
    "qemu-img",
    "rpm",
    "dnf",
)
HOST_FAMILIES = (
    "fedora",
    "rhel",
    "centos",
    "almalinux",
    "rocky",
    "oracle",
)
LIBEXEC_TOOLS = {
    "osbuild-depsolve-dnf": (
        "/usr/libexec/osbuild-depsolve-dnf",
        "/usr/lib/osbuild/osbuild-depsolve-dnf",
    ),
}


def _tool_path(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for candidate in LIBEXEC_TOOLS.get(name, ()):
        if Path(candidate).is_file():
            return candidate
    return None


def _os_release() -> dict[str, str]:
    try:
        return platform.freedesktop_os_release()
    except OSError:
        return {}


def _host_name() -> str:
    info = _os_release()
    return info.get("PRETTY_NAME") or info.get("NAME") or "Linux host"


def _host_supported() -> bool:
    info = _os_release()
    family = " ".join(
        (info.get("ID", ""), info.get("ID_LIKE", ""))
    ).lower()
    return any(name in family for name in HOST_FAMILIES)


def _terminate_process(process: subprocess.Popen, timeout: float = 5.0) -> None:
    """Stop a process started in its own session, then reap it."""

    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=timeout)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


@dataclass(frozen=True)
class HostStatus:
    ready: bool
    summary: str
    detail: str = ""
    supported: bool = True


@dataclass(frozen=True)
class BlueprintRecord:
    name: str
    version: str
    distro: str
    desktop: str
    path: str
    sha256: str
    last_build: str = "—"
    kind: str = "qcow2"


@dataclass
class ImageRecord:
    name: str
    target: str
    state: str
    created: str
    size: str
    path: str


@dataclass(frozen=True)
class PreflightCheck:
    label: str
    state: str
    detail: str = ""

    @property
    def blocking(self) -> bool:
        return self.state == "error"


@dataclass(frozen=True)
class PreflightResult:
    checks: list[PreflightCheck] = field(default_factory=list)
    packages: int = 0

    @property
    def blocking(self) -> bool:
        return any(check.blocking for check in self.checks)

    @property
    def warnings(self) -> int:
        return sum(check.state == "warning" for check in self.checks)


@dataclass
class BuildJob:
    image: ImageRecord
    command: list[str]
    log_path: Path
    process: subprocess.Popen | None = None
    state: str = "Queued"
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    preview: bool = False
    expected_packages: int = 0

    def poll(self) -> str:
        if (
            self.process is not None
            and self.state in {"Queued", "Running"}
        ):
            code = self.process.poll()
            if code is not None:
                self._finish("Succeeded" if code == 0 else "Failed")
        return self.state

    def _finish(self, state: str) -> None:
        if self.state in {"Succeeded", "Failed", "Cancelled"}:
            return
        self.state = state
        self.finished = time.monotonic()
        self.image.state = state
        if state == "Succeeded":
            artifacts = [
                item
                for item in self.log_path.parent.iterdir()
                if item.is_file()
                and item.name != "updates.img"
                and item.suffix.lower() in {".qcow2", ".iso", ".raw", ".img"}
            ]
            if artifacts:
                artifact = max(artifacts, key=lambda item: item.stat().st_size)
                self.image.path = str(artifact)
                size = artifact.stat().st_size / (1024**3)
                self.image.size = f"{size:.1f} GB"
                self._cleanup_artifacts(artifact)

    def _cleanup_artifacts(self, artifact: Path) -> None:
        """Keep only the final artifact and the build log.

        The ISO helper already removes its root-owned work directories before
        exiting. This fallback covers qcow2 builds and future intermediates;
        permission errors are ignored so a successful build stays successful.
        """
        keep = {artifact.resolve(), self.log_path.resolve()}
        for entry in list(self.log_path.parent.iterdir()):
            try:
                if entry.resolve() in keep:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink(missing_ok=True)
            except OSError:
                continue

    def elapsed(self) -> float:
        end = self.finished if self.finished is not None else time.monotonic()
        return max(0.0, end - self.started)

    def cancel(self) -> None:
        if self.state in {"Succeeded", "Failed", "Cancelled"}:
            return
        if self.process is not None and self.process.poll() is None:
            _terminate_process(self.process)
        self._finish("Cancelled")


class FakeImageBuilderAdapter:
    """Preview adapter used until the real image-builder adapter lands."""

    def __init__(self) -> None:
        self._blueprints: dict[str, BlueprintRecord] = {}
        self._images: list[ImageRecord] = []
        self._next_image = 1

    def host_status(self) -> HostStatus:
        return HostStatus(
            ready=True,
            summary=f"{_host_name()} · image-builder 87.0.0 (preview)",
            detail="Preview adapter; real host/tool detection is next.",
        )

    def blueprints(self) -> list[BlueprintRecord]:
        return sorted(
            self._blueprints.values(), key=lambda item: item.name.lower()
        )

    def images(self) -> list[ImageRecord]:
        return list(self._images)

    def catalog(self, *, refresh: bool = False) -> list[dict[str, str]]:
        """Return buildable distro/arch/image-type combinations."""
        return [
            {
                "distro": "fedora-45",
                "arch": platform.machine(),
                "image_type": "generic-qcow2",
            },
            {
                "distro": "fedora-45",
                "arch": platform.machine(),
                "image_type": "minimal-installer",
            },
            {
                "distro": "almalinux-10.2",
                "arch": platform.machine(),
                "image_type": "qcow2",
            },
            {
                "distro": "almalinux-10.2",
                "arch": platform.machine(),
                "image_type": "image-installer",
            },
        ]

    def default_output_dir(self, mode: BuildMode) -> str:
        return str(Path.home() / "vEMU-Images")

    def preflight(
        self,
        mode: BuildMode,
        blueprint: BlueprintRecord,
        output_dir: str,
        cancel: threading.Event | None = None,
        iso_options: IsoOptions | None = None,
    ) -> PreflightResult:
        checks: list[PreflightCheck] = []
        try:
            self.validate_blueprint(blueprint.path)
            checks.append(
                PreflightCheck(
                    "Blueprint", "ok", f"{blueprint.name} is readable"
                )
            )
        except ValidationError as exc:
            checks.append(PreflightCheck("Blueprint", "error", str(exc)))

        checks.append(
            PreflightCheck(
                "Host tools",
                "ok",
                "Preview adapter; real image-builder detection is next",
            )
        )
        checks.append(
            PreflightCheck(
                "Packages and repos",
                "warning",
                "Preview adapter: manifest/package resolution is not wired yet",
            )
        )

        target = Path(output_dir)
        parent = target
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        try:
            free = shutil.disk_usage(parent).free
            free_gib = free / (1024**3)
            required_gib = 6.0 if mode == "qcow2" else 5.0
            if free_gib < required_gib:
                checks.append(
                    PreflightCheck(
                        "Disk space",
                        "error",
                        f"{free_gib:.1f} GiB free; {required_gib:.1f} GiB required",
                    )
                )
            else:
                checks.append(
                    PreflightCheck(
                        "Disk space",
                        "ok",
                        f"{free_gib:.1f} GiB free; {required_gib:.1f} GiB required",
                    )
                )
        except OSError as exc:
            checks.append(PreflightCheck("Disk space", "error", str(exc)))

        if mode == "qcow2":
            checks.append(
                PreflightCheck(
                    "SELinux",
                    "ok",
                    "qcow2 target uses the managed images role (preview)",
                )
            )
        else:
            checks.append(
                PreflightCheck(
                    "ISO path", "ok", "User-selected file; not managed"
                )
            )

        checks.append(
            PreflightCheck(
                "Firstboot",
                "ok",
                "Firstboot scripts are not executed during the build",
            )
        )
        privilege = shutil.which("pkexec") or shutil.which("sudo")
        checks.append(
            PreflightCheck(
                "Privilege",
                "ok" if privilege else "error",
                privilege or "No pkexec/sudo available",
            )
        )
        return PreflightResult(checks)

    @staticmethod
    def _selinux_preflight(output_dir: str) -> PreflightCheck:
        from .storage import inspect_location

        target = Path(output_dir)
        existing = target
        while not existing.exists() and existing != existing.parent:
            existing = existing.parent
        try:
            location = inspect_location(existing, "images")
        except (OSError, ValidationError) as exc:
            return PreflightCheck(
                "SELinux",
                "warning",
                f"Could not inspect {existing}: {exc}",
            )
        if location.selinux_mode not in {"Enforcing", "Permissive"}:
            return PreflightCheck(
                "SELinux",
                "ok",
                f"SELinux {location.selinux_mode}; label check skipped",
            )
        if location.selinux_label == location.expected_label:
            return PreflightCheck(
                "SELinux",
                "ok",
                f"{existing} is labeled {location.selinux_label}",
            )
        return PreflightCheck(
            "SELinux",
            "warning",
            f"{existing} is {location.selinux_label or 'unlabeled'}; "
            f"vEMU registers the images role and applies "
            f"{location.expected_label} after build.",
        )

    def start_build(
        self,
        mode: BuildMode,
        blueprint: BlueprintRecord,
        output_dir: str,
        expected_packages: int = 0,
        iso_options: IsoOptions | None = None,
    ) -> BuildJob:
        job_id = f"{blueprint.name}-{mode}-{self._next_image}"
        job_dir = Path(output_dir) / job_id
        image = ImageRecord(
            name=blueprint.name,
            target=f"{blueprint.distro} / {mode}",
            state="Queued",
            created=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
            size="—",
            path=str(job_dir),
        )
        self._images.insert(0, image)
        self._next_image += 1
        return BuildJob(
            image=image,
            command=[],
            log_path=job_dir / "build.log",
            state="Queued",
            preview=True,
            expected_packages=expected_packages,
        )

    def import_blueprint(self, path: str | Path) -> BlueprintRecord:
        source = Path(path).expanduser()
        try:
            payload = tomllib.loads(source.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValidationError(
                f"Cannot read blueprint {source}: {exc}"
            ) from exc
        source = source.resolve()
        name = str(payload.get("name") or source.stem)
        version = str(payload.get("version") or "")
        distro = str(payload.get("distro") or "host")
        text = source.read_text(encoding="utf-8").lower()
        desktop = (
            "KDE" if "kde" in text else "GNOME" if "gnome" in text else ""
        )
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        record = BlueprintRecord(
            name=name,
            version=version,
            distro=distro,
            desktop=desktop,
            path=str(source),
            sha256=digest,
            kind="iso" if source.stem.endswith("-iso") else "qcow2",
        )
        self._blueprints[str(source)] = record
        return record

    def validate_blueprint(self, path: str | Path) -> list[str]:
        source = Path(path)
        try:
            tomllib.loads(source.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValidationError(
                f"Cannot validate blueprint {source}: {exc}"
            ) from exc
        return []

    def remove_blueprint(self, path: str | Path) -> None:
        self._blueprints.pop(str(path), None)

    def forget_image(self, image: ImageRecord) -> None:
        try:
            self._images.remove(image)
        except ValueError:
            pass

    def adopt_image(self, image: ImageRecord) -> None:
        if any(item.path == image.path for item in self._images):
            return
        self._images.insert(0, image)


class RealImageBuilderAdapter(FakeImageBuilderAdapter):
    """Adapter for a Fedora/RHEL-family host with image-builder installed.

    The registry/import methods are shared with the fake adapter.  Host
    detection, manifest preflight, build-command construction and the
    privileged build job are real.
    """

    def __init__(
        self,
        repo_dir: str | Path | None = None,
        *,
        metadata: ImageMetadataStore | None = None,
    ) -> None:
        super().__init__()
        self._catalog_cache: list[dict[str, str]] | None = None
        self._metadata = metadata
        self._engine_version: str | None = None
        if repo_dir is None:
            repo_dir = os.environ.get("VEMU_IMAGE_BUILDER_REPO_DIR")
        if not repo_dir:
            candidate = Path.home() / ".config/vemu/image-builder/repositories"
            if candidate.is_dir():
                repo_dir = candidate
        self.repo_dir = str(repo_dir) if repo_dir else None

    def host_status(self) -> HostStatus:
        host = _host_name()
        if not _host_supported():
            return HostStatus(
                ready=False,
                supported=False,
                summary=f"{host} · unsupported host",
                detail=(
                    "Image Builder only supports Fedora and RHEL-family hosts."
                ),
            )
        missing = [
            tool for tool in REQUIRED_TOOLS if _tool_path(tool) is None
        ]
        if missing:
            return HostStatus(
                ready=False,
                summary=f"{host} · missing {', '.join(missing)}",
                detail=(
                    "Install image-builder, osbuild, osbuild-depsolve-dnf, "
                    "qemu-img, rpm and dnf."
                ),
            )
        executable = shutil.which("image-builder") or "image-builder"
        return HostStatus(
            ready=True,
            summary=f"{host} · image-builder {self._version(executable)}",
            detail=executable,
        )

    def catalog(self, *, refresh: bool = False) -> list[dict[str, str]]:
        """List image-builder's buildable distros, arches and image types."""
        if self._catalog_cache is not None and not refresh:
            return [dict(entry) for entry in self._catalog_cache]
        if not _host_supported():
            raise ValidationError(
                "Image Builder only supports Fedora and RHEL-family hosts."
            )
        executable = shutil.which("image-builder")
        if executable is None:
            raise ValidationError(
                "image-builder was not found. Install image-builder to browse "
                "the OSBuild catalog."
            )
        engine_version = "unknown"
        cache_host = host_cache_id()
        if self._metadata is not None:
            if refresh or self._engine_version is None:
                self._engine_version = self._version(executable)
            engine_version = self._engine_version
        if self._metadata is not None and not refresh:
            cached = self._metadata.catalog_get(
                cache_host, engine_version, ttl=CATALOG_TTL
            )
            if cached is not None:
                self._catalog_cache = cached
                return [dict(entry) for entry in cached]
        try:
            result = subprocess.run(
                [executable, "list", "--format", "json"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            stale = self._stale_catalog(cache_host, engine_version)
            if stale is not None:
                return stale
            raise ValidationError(
                f"Cannot run image-builder list: {exc}"
            ) from exc
        if result.returncode != 0:
            stale = self._stale_catalog(cache_host, engine_version)
            if stale is not None:
                return stale
            detail = (result.stderr or result.stdout).strip().splitlines()
            message = detail[-1] if detail else "unknown error"
            raise ValidationError(
                f"image-builder list failed: {message}"
            )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ValidationError(
                f"Cannot parse image-builder list output: {exc}"
            ) from exc
        entries: list[dict[str, str]] = []
        for item in payload if isinstance(payload, list) else []:
            try:
                entries.append(
                    {
                        "distro": str(item["distro"]["name"]),
                        "arch": str(item["arch"]["name"]),
                        "image_type": str(item["image_type"]["name"]),
                    }
                )
            except (KeyError, TypeError):
                continue
        entries.sort(
            key=lambda entry: (
                entry["distro"],
                entry["arch"],
                entry["image_type"],
            )
        )
        if self._metadata is not None:
            try:
                self._metadata.catalog_put(
                    cache_host, engine_version, entries
                )
            except (OSError, ValidationError):
                # A cache write must never hide a valid catalog.
                pass
        self._catalog_cache = entries
        return [dict(entry) for entry in entries]

    def _stale_catalog(
        self, cache_host: str, engine_version: str
    ) -> list[dict[str, str]] | None:
        if self._metadata is None:
            return None
        try:
            stale = self._metadata.catalog_get_stale(
                cache_host, engine_version
            )
        except (OSError, ValidationError):
            return None
        if stale is None:
            return None
        self._catalog_cache = stale
        return [dict(entry) for entry in stale]

    @staticmethod
    def _version(executable: str) -> str:
        for flag in ("version", "--version"):
            try:
                result = subprocess.run(
                    [executable, flag],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            output = f"{result.stdout}\n{result.stderr}"
            for line in output.splitlines():
                line = line.strip()
                if line.startswith("version:"):
                    return line.split(":", 1)[1].strip().strip('"')
            if result.returncode == 0:
                first = result.stdout.strip().splitlines()
                if first:
                    return first[0].strip()
        return "unknown"

    @staticmethod
    def image_type(mode: BuildMode, distro: str) -> str:
        name = distro.lower()
        if name.startswith("fedora"):
            return "generic-qcow2" if mode == "qcow2" else "minimal-installer"
        if any(
            family in name
            for family in ("rhel", "almalinux", "rocky", "centos", "oracle")
        ):
            return "qcow2" if mode == "qcow2" else "image-installer"
        raise ValidationError(
            f"Cannot choose an image type for distro '{distro}'."
        )

    @staticmethod
    def _blueprint_payload(blueprint: BlueprintRecord) -> dict:
        source = Path(blueprint.path)
        try:
            return tomllib.loads(source.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ValidationError(
                f"Cannot read blueprint {source}: {exc}"
            ) from exc

    def extra_repos(self, blueprint: BlueprintRecord) -> list[str]:
        payload = self._blueprint_payload(blueprint)
        customizations = payload.get("customizations", {})
        if not isinstance(customizations, dict):
            return []
        repositories = customizations.get("repositories", [])
        if not isinstance(repositories, list):
            return []
        urls: list[str] = []
        for repo in repositories:
            if not isinstance(repo, dict):
                continue
            if repo.get("enabled", True) is False:
                continue
            for url in repo.get("baseurls", []):
                if url and str(url) not in urls:
                    urls.append(str(url))
        return urls

    def build_command(
        self,
        mode: BuildMode,
        blueprint: BlueprintRecord,
        output_dir: str,
    ) -> list[str]:
        executable = shutil.which("image-builder") or "image-builder"
        command = [
            executable,
            "build",
            self.image_type(mode, blueprint.distro),
            "--distro",
            blueprint.distro,
            "--blueprint",
            blueprint.path,
            "--output-dir",
            output_dir,
        ]
        if self.repo_dir:
            command.extend(["--force-repo-dir", self.repo_dir])
        for url in self.extra_repos(blueprint):
            command.extend(["--extra-repo", url])
        return command

    @staticmethod
    def _manifest_packages(manifest: str) -> int:
        try:
            payload = json.loads(manifest)
        except (TypeError, json.JSONDecodeError):
            return 0
        sources = payload.get("sources", {})
        if not isinstance(sources, dict):
            return 0
        librepo = sources.get("org.osbuild.librepo", {})
        if not isinstance(librepo, dict):
            return 0
        items = librepo.get("items", {})
        return len(items) if isinstance(items, dict) else 0

    @staticmethod
    def _tail(text: str, limit: int = 700) -> str:
        text = (text or "").strip()
        return text[-limit:] if len(text) > limit else text

    @staticmethod
    def _run_manifest(
        command: list[str],
        *,
        cancel: threading.Event | None = None,
        timeout: float = 240.0,
    ) -> tuple[int, str, str]:
        """Resolve a manifest without blocking cancellation."""

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return -1, "", str(exc)
        deadline = time.monotonic() + timeout
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.25)
                return process.returncode or 0, stdout, stderr
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and cancel.is_set():
                _terminate_process(process)
                return -1, "", "Preflight cancelled."
            if time.monotonic() >= deadline:
                _terminate_process(process)
                return -1, "", f"Preflight timed out after {timeout:.0f}s."

    def preflight(
        self,
        mode: BuildMode,
        blueprint: BlueprintRecord,
        output_dir: str,
        cancel: threading.Event | None = None,
        iso_options: IsoOptions | None = None,
    ) -> PreflightResult:
        checks: list[PreflightCheck] = []
        host = self.host_status()
        packages = 0
        checks.append(
            PreflightCheck(
                "Host tools",
                "ok" if host.ready else "error",
                host.summary + (f" · {host.detail}" if host.detail else ""),
            )
        )

        try:
            self.validate_blueprint(blueprint.path)
            checks.append(
                PreflightCheck(
                    "Blueprint", "ok", f"{blueprint.name} is readable"
                )
            )
        except ValidationError as exc:
            checks.append(PreflightCheck("Blueprint", "error", str(exc)))


        if self.repo_dir:
            exists = Path(self.repo_dir).is_dir()
            checks.append(
                PreflightCheck(
                    "Repository overrides",
                    "ok" if exists else "error",
                    self.repo_dir
                    if exists
                    else f"Repository override directory not found: {self.repo_dir}",
                )
            )

        if blueprint.distro.lower().startswith("rhel-"):
            host_id = _os_release().get("ID", "").lower()
            if host_id == "rhel":
                checks.append(
                    PreflightCheck(
                        "RHEL subscription",
                        "ok",
                        "RHEL host; manifest verifies subscription access",
                    )
                )
            elif self.repo_dir:
                checks.append(
                    PreflightCheck(
                        "RHEL subscription",
                        "ok",
                        f"Using repository overrides: {self.repo_dir}",
                    )
                )
            else:
                checks.append(
                    PreflightCheck(
                        "RHEL subscription",
                        "error",
                        "RHEL targets need a registered RHEL host with "
                        "BaseOS/AppStream (and CRB for KDE). Build on RHEL "
                        "or set VEMU_IMAGE_BUILDER_REPO_DIR to "
                        "entitlement-backed repository overrides.",
                    )
                )

        try:
            image_type = self.image_type(mode, blueprint.distro)
            if not host.ready:
                raise ValidationError("host environment is not ready")
            executable = shutil.which("image-builder") or "image-builder"
            command = [
                executable,
                "manifest",
                image_type,
                "--distro",
                blueprint.distro,
                "--blueprint",
                blueprint.path,
            ]
            if self.repo_dir:
                command.extend(["--force-repo-dir", self.repo_dir])
            for url in self.extra_repos(blueprint):
                command.extend(["--extra-repo", url])
            code, stdout, stderr = self._run_manifest(
                command, cancel=cancel
            )
            packages = self._manifest_packages(stdout) if code == 0 else 0
            if code == 0:
                checks.append(
                    PreflightCheck(
                        "Packages and repos",
                        "ok",
                        f"{image_type} manifest resolved",
                    )
                )
            else:
                checks.append(
                    PreflightCheck(
                        "Packages and repos",
                        "error",
                        self._tail(stderr or stdout) or "manifest failed",
                    )
                )
        except (OSError, subprocess.SubprocessError, ValidationError) as exc:
            checks.append(
                PreflightCheck("Packages and repos", "error", str(exc))
            )

        target = Path(output_dir)
        parent = target
        while not parent.exists() and parent != parent.parent:
            parent = parent.parent
        try:
            free = shutil.disk_usage(parent).free
            free_gib = free / (1024**3)
            required_gib = 6.0 if mode == "qcow2" else 5.0
            checks.append(
                PreflightCheck(
                    "Disk space",
                    "ok" if free_gib >= required_gib else "error",
                    f"{free_gib:.1f} GiB free; {required_gib:.1f} GiB required",
                )
            )
        except OSError as exc:
            checks.append(PreflightCheck("Disk space", "error", str(exc)))

        if mode == "qcow2":
            checks.append(self._selinux_preflight(output_dir))
        else:
            checks.append(
                PreflightCheck(
                    "ISO path", "ok", "User-selected file; not managed"
                )
            )
            options = iso_options or IsoOptions()
            try:
                iso_builder.validate_options(options)
                checks.append(
                    PreflightCheck(
                        "ISO customization",
                        "ok",
                        (
                            "Branding + firstboot"
                            if options.branding and options.firstboot
                            else "Branding"
                            if options.branding
                            else "Firstboot"
                            if options.firstboot
                            else "Cleanup kickstart only"
                        ),
                    )
                )
            except ValueError as exc:
                checks.append(PreflightCheck("ISO customization", "error", str(exc)))

            required = ["mkksiso", "xorriso"]
            if options.branding:
                required += ["cpio"]
                if not (shutil.which("magick") or shutil.which("convert")):
                    required.append("magick/convert")
            missing = [tool for tool in required if not shutil.which(tool)]
            checks.append(
                PreflightCheck(
                    "ISO tools",
                    "error" if missing else "ok",
                    "Missing: " + ", ".join(missing)
                    if missing
                    else "mkksiso/xorriso ready",
                )
            )
            if options.firstboot:
                try:
                    text = Path(blueprint.path).read_text(
                        encoding="utf-8", errors="replace"
                    ).lower()
                except OSError:
                    text = ""
                if "python3" not in text:
                    checks.append(
                        PreflightCheck(
                            "Firstboot runtime",
                            "warning",
                            "Firstboot TUI needs python3 in the installed "
                            "image; add python3 to the blueprint if the "
                            "image is minimal.",
                        )
                    )
                else:
                    checks.append(
                        PreflightCheck(
                            "Firstboot runtime",
                            "ok",
                            "python3 is present in the blueprint",
                        )
                    )
            if options.branding:
                try:
                    text = Path(blueprint.path).read_text(
                        encoding="utf-8", errors="replace"
                    ).lower()
                except OSError:
                    text = ""
                if "plymouth-plugin-two-step" not in text:
                    checks.append(
                        PreflightCheck(
                            "Plymouth runtime",
                            "warning",
                            "Branded Plymouth needs plymouth-plugin-two-step "
                            "and plymouth-scripts in the ISO blueprint.",
                        )
                    )
                else:
                    checks.append(
                        PreflightCheck(
                            "Plymouth runtime",
                            "ok",
                            "Plymouth two-step plugin is in the blueprint",
                        )
                    )

        try:
            payload = self._blueprint_payload(blueprint)
            customizations = payload.get("customizations", {})
            firstboot = (
                customizations.get("firstboot", {})
                if isinstance(customizations, dict)
                else {}
            )
            scripts = (
                firstboot.get("scripts", [])
                if isinstance(firstboot, dict)
                else []
            )
            checks.append(
                PreflightCheck(
                    "Firstboot",
                    "ok",
                    f"{len(scripts)} script(s); not executed during build",
                )
            )
        except ValidationError as exc:
            checks.append(PreflightCheck("Firstboot", "error", str(exc)))

        privilege = shutil.which("pkexec") or shutil.which("sudo")
        checks.append(
            PreflightCheck(
                "Privilege",
                "ok" if privilege else "error",
                privilege or "No pkexec/sudo available",
            )
        )
        return PreflightResult(checks, packages=packages)

    def start_build(
        self,
        mode: BuildMode,
        blueprint: BlueprintRecord,
        output_dir: str,
        expected_packages: int = 0,
        iso_options: IsoOptions | None = None,
    ) -> BuildJob:
        job_id = (
            f"{blueprint.name}-{mode}-"
            f"{datetime.now().astimezone().strftime('%Y%m%d-%H%M%S')}"
        )
        job_dir = Path(output_dir) / job_id
        try:
            job_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ValidationError(
                f"Cannot create output directory {job_dir}: {exc}"
            ) from exc
        if mode == "iso":
            options = iso_options or IsoOptions()
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", blueprint.name).strip("-") or "vemu"
            final_iso = job_dir / f"{safe_name}-{blueprint.distro}-iso.iso"
            try:
                iso_builder.prepare_job(job_dir, options, output=final_iso)
            except (OSError, ValueError, RuntimeError) as exc:
                raise ValidationError(f"Cannot prepare ISO customization: {exc}") from exc
            command = [
                shutil.which("python3") or sys.executable,
                str(Path(iso_builder.__file__).resolve()),
                "--job-dir",
                str(job_dir),
                "--blueprint",
                blueprint.path,
                "--distro",
                blueprint.distro,
                "--output",
                str(final_iso),
                "--options",
                str(job_dir / "iso-options.json"),
            ]
            if self.repo_dir:
                command += ["--force-repo-dir", self.repo_dir]
            for url in self.extra_repos(blueprint):
                command += ["--extra-repo", url]
        else:
            command = self.build_command(mode, blueprint, str(job_dir))
        if os.geteuid() != 0:
            pkexec = shutil.which("pkexec")
            if pkexec is None:
                raise ValidationError(
                    "Building requires administrator approval, but pkexec "
                    "was not found."
                )
            command = [pkexec, *command]
        image = ImageRecord(
            name=blueprint.name,
            target=f"{blueprint.distro} / {mode}",
            state="Running",
            created=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
            size="—",
            path=str(job_dir),
        )
        log_path = job_dir / "build.log"
        try:
            with log_path.open("wb") as log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
        except OSError as exc:
            raise ValidationError(
                f"Cannot start image-builder: {exc}"
            ) from exc
        self._images.insert(0, image)
        return BuildJob(
            image=image,
            command=command,
            log_path=log_path,
            process=process,
            state="Running",
            expected_packages=expected_packages,
        )


def default_adapter() -> RealImageBuilderAdapter:
    """Use the real adapter; the page gates unsupported/missing hosts.

    The fake adapter is only for tests and explicit preview use, so a
    Debian/openSUSE host cannot accidentally enter a preview build path.
    """

    return RealImageBuilderAdapter(metadata=ImageMetadataStore())


__all__ = [
    "BlueprintRecord",
    "BuildJob",
    "BuildMode",
    "FakeImageBuilderAdapter",
    "HostStatus",
    "ImageRecord",
    "PreflightCheck",
    "PreflightResult",
    "RealImageBuilderAdapter",
    "default_adapter",
]
