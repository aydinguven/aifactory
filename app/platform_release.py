"""Validated metadata and publication helpers for immutable platform updates."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tarfile
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.installer.platform import InstallerError


# Releases up to 3.49.x are published as devcloud-platform-update-*; later
# ones as aifactory-platform-update-*. Both are accepted.
PLATFORM_BUNDLE_PATTERN = re.compile(
    r"^(?:aifactory|devcloud)-platform-update-v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
    r"(?:-(?P<revision>[0-9a-f]{7,40}))?\.tar\.gz$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class PlatformImage:
    role: str
    image: str
    source: str
    digest: str
    archive: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class PlatformRelease:
    version: str
    source_commit: str
    controller: PlatformImage
    worker: PlatformImage


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_archive_path(raw: object, expected: set[str]) -> str:
    value = str(raw or "").replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or value not in expected:
        raise InstallerError(f"Platform image archive path is invalid: {value}")
    return value


def _image(root: Path, value: object, role: str) -> PlatformImage:
    if not isinstance(value, dict):
        raise InstallerError(f"Platform manifest has no {role} image")
    expected = {
        f"offline/{role}-images/{prefix}-{role}.tar"
        for prefix in ("aifactory", "devcloud")
    }
    archive_name = _safe_archive_path(value.get("archive"), expected)
    archive = root.joinpath(*PurePosixPath(archive_name).parts)
    checksum = str(value.get("sha256") or "")
    size = int(value.get("size") or 0)
    digest = str(value.get("digest") or "")
    image = str(value.get("image") or "").strip()
    source = str(value.get("source") or "").strip()
    if not image or not source:
        raise InstallerError(f"Platform {role} image references are missing")
    if not IMAGE_DIGEST_PATTERN.fullmatch(digest):
        raise InstallerError(f"Platform {role} image digest is invalid")
    if not SHA256_PATTERN.fullmatch(checksum):
        raise InstallerError(f"Platform {role} archive checksum is invalid")
    if not archive.is_file() or archive.is_symlink():
        raise InstallerError(f"Platform {role} image archive is missing")
    if archive.stat().st_size != size or sha256_file(archive) != checksum:
        raise InstallerError(f"Platform {role} image archive verification failed")
    return PlatformImage(role, image, source, digest, archive_name, checksum, size)


def load_platform_release(root: Path) -> PlatformRelease:
    path = root / "platform-release.json"
    if not path.is_file() or path.is_symlink():
        raise InstallerError("Platform release manifest is missing")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallerError(f"Platform release manifest is invalid JSON: {exc}") from exc
    if not isinstance(value, dict) or value.get("format") != 1:
        raise InstallerError("Platform release manifest format is unsupported")
    version = str(value.get("version") or "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise InstallerError("Platform release version is invalid")
    source_commit = str(value.get("source_commit") or "")
    if not re.fullmatch(r"[0-9a-f]{7,40}", source_commit):
        raise InstallerError("Platform release source commit is invalid")
    if value.get("workspace_images_included") is not False:
        raise InstallerError("Platform updates must not contain workspace images")
    images = value.get("images")
    if not isinstance(images, dict):
        raise InstallerError("Platform release image index is missing")
    return PlatformRelease(
        version=version,
        source_commit=source_commit,
        controller=_image(root, images.get("controller"), "controller"),
        worker=_image(root, images.get("worker"), "worker"),
    )


# Room left after a copy so the host keeps working.
PUBLISH_FREE_MARGIN_BYTES = 256 * 1024 * 1024


def _format_size(value: int) -> str:
    return f"{value / (1024 ** 3):.1f} GiB"


def _bundle_sort_key(path: Path) -> tuple[tuple[int, ...], int, str]:
    match = PLATFORM_BUNDLE_PATTERN.fullmatch(path.name)
    version = tuple(int(part) for part in match.group("version").split(".")) if match else (0,)
    return version, path.stat().st_mtime_ns, path.name


def prune_published_bundles(release_root: Path, *, keep: int, protect: str = "") -> list[Path]:
    """Delete all but the ``keep`` newest published bundles (and leftovers).

    Workers only download the newest bundle, so older ones only use disk.
    Interrupted copies (``.*.partial``) are always removed.
    """
    removed: list[Path] = []
    if not release_root.is_dir():
        return removed
    for partial in [
        *release_root.glob(".aifactory-platform-update-*.partial"),
        *release_root.glob(".devcloud-platform-update-*.partial"),
    ]:
        partial.unlink(missing_ok=True)
        removed.append(partial)
    bundles = sorted(
        (
            path
            for path in release_root.iterdir()
            if path.is_file()
            and not path.is_symlink()
            and PLATFORM_BUNDLE_PATTERN.fullmatch(path.name)
            and path.name != protect
        ),
        key=_bundle_sort_key,
        reverse=True,
    )
    for path in bundles[max(0, keep):]:
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".sha256").unlink(missing_ok=True)
        removed.append(path)
    return removed


def publish_platform_bundle(bundle: Path, downloads_root: Path, *, keep_previous: int = 1) -> Path:
    """Atomically publish one verified bundle for enrolled worker upgrades.

    Older published bundles beyond ``keep_previous`` are removed first, and
    the copy only starts when the bundle fits on the disk.
    """
    if not PLATFORM_BUNDLE_PATTERN.fullmatch(bundle.name):
        raise InstallerError("Platform update filename is invalid")
    release_root = downloads_root.resolve() / "releases"
    release_root.mkdir(parents=True, exist_ok=True)
    target = release_root / bundle.name
    prune_published_bundles(release_root, keep=keep_previous, protect=bundle.name)
    needed = bundle.stat().st_size + PUBLISH_FREE_MARGIN_BYTES
    free = shutil.disk_usage(release_root).free
    if free < needed:
        raise InstallerError(
            f"Not enough free disk space in {release_root} to publish the worker "
            f"update bundle: about {_format_size(needed)} needed, "
            f"{_format_size(free)} free"
        )
    temporary = release_root / f".{target.name}.{uuid.uuid4().hex}.partial"
    try:
        with bundle.open("rb") as source, temporary.open("xb") as destination:
            shutil.copyfileobj(source, destination, length=1024 * 1024)
        if sha256_file(temporary) != sha256_file(bundle):
            raise InstallerError("Published platform bundle checksum mismatch")
        temporary.replace(target)
        target.with_name(target.name + ".sha256").write_text(
            f"{sha256_file(target)}  {target.name}\n", encoding="ascii"
        )
    except OSError as exc:
        raise InstallerError(f"Could not publish the worker update bundle: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _verified_release_files(root: Path, release: PlatformRelease) -> list[Path]:
    manifest_path = root / "release.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise InstallerError("Release manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallerError(f"Release manifest is invalid JSON: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != 1
        or manifest.get("version") != release.version
        or manifest.get("source_commit") != release.source_commit
        or not isinstance(manifest.get("artifacts"), list)
    ):
        raise InstallerError("Release manifest does not match the platform release")

    files = [manifest_path]
    signature = root / "release.json.asc"
    if signature.is_file() and not signature.is_symlink():
        files.append(signature)
    seen: set[str] = set()
    for entry in manifest["artifacts"]:
        if not isinstance(entry, dict):
            raise InstallerError("Release artifact index is invalid")
        raw = str(entry.get("path") or "").replace("\\", "/")
        relative = PurePosixPath(raw)
        if (
            not raw
            or relative.is_absolute()
            or ".." in relative.parts
            or raw in seen
        ):
            raise InstallerError(f"Release artifact path is invalid: {raw}")
        seen.add(raw)
        path = root.joinpath(*relative.parts)
        checksum = str(entry.get("sha256") or "")
        size = int(entry.get("size") or -1)
        if (
            not path.is_file()
            or path.is_symlink()
            or not SHA256_PATTERN.fullmatch(checksum)
            or path.stat().st_size != size
            or sha256_file(path) != checksum
        ):
            raise InstallerError(f"Release artifact verification failed: {raw}")
        files.append(path)
    return files


def publish_platform_root(root: Path, downloads_root: Path) -> Path:
    """Publish a clean-install platform tree as an authenticated worker bundle."""
    release = load_platform_release(root)
    files = _verified_release_files(root, release)
    release_root = downloads_root.resolve() / "releases"
    release_root.mkdir(parents=True, exist_ok=True)
    # Workers up to 3.49.x check this name; the aifactory name follows once
    # every host runs 3.49.1 or later.
    filename = (
        f"devcloud-platform-update-v{release.version}-"
        f"{release.source_commit[:12]}.tar.gz"
    )
    target = release_root / filename
    temporary = release_root / f".{filename}.{uuid.uuid4().hex}.partial"
    archive_root = f"devcloud-{release.version}"
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for path in files:
                relative = path.relative_to(root).as_posix()
                archive.add(
                    path,
                    arcname=f"{archive_root}/{relative}",
                    recursive=False,
                )
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    target.with_name(target.name + ".sha256").write_text(
        f"{sha256_file(target)}  {target.name}\n", encoding="ascii"
    )
    return target
