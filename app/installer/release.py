from __future__ import annotations

import hashlib
import json
import shutil
import stat
import tarfile
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from app.installer.platform import CommandRunner, InstallerError


MAX_RELEASE_BYTES = 32 * 1024 * 1024 * 1024
MAX_RELEASE_ENTRIES = 100_000


@dataclass(frozen=True, slots=True)
class PreparedRelease:
    root: Path
    version: str
    manifest: dict | None
    signature_verified: bool


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative(raw: str) -> PurePosixPath:
    normalized = raw.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise InstallerError(f"Unsafe release path: {raw}")
    return path


def _extract_zip(archive: Path, destination: Path) -> None:
    total = 0
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > MAX_RELEASE_ENTRIES:
            raise InstallerError("Release contains too many archive entries")
        for entry in entries:
            relative = _safe_relative(entry.filename)
            mode = entry.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise InstallerError(f"Release contains a symbolic link: {entry.filename}")
            total += entry.file_size
            if total > MAX_RELEASE_BYTES:
                raise InstallerError("Expanded release exceeds the size limit")
            target = destination.joinpath(*relative.parts)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(entry) as input_file, target.open("wb") as output_file:
                shutil.copyfileobj(input_file, output_file)


def _extract_tar(archive: Path, destination: Path) -> None:
    total = 0
    with tarfile.open(archive, "r:*") as source:
        entries = source.getmembers()
        if len(entries) > MAX_RELEASE_ENTRIES:
            raise InstallerError("Release contains too many archive entries")
        for entry in entries:
            relative = _safe_relative(entry.name)
            if entry.issym() or entry.islnk() or entry.isdev():
                raise InstallerError(f"Release contains an unsupported link or device: {entry.name}")
            total += max(0, entry.size)
            if total > MAX_RELEASE_BYTES:
                raise InstallerError("Expanded release exceeds the size limit")
            target = destination.joinpath(*relative.parts)
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not entry.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            input_file = source.extractfile(entry)
            if input_file is None:
                raise InstallerError(f"Cannot read release entry: {entry.name}")
            with input_file, target.open("wb") as output_file:
                shutil.copyfileobj(input_file, output_file)


def _source_root(extracted: Path) -> Path:
    if (extracted / "app" / "__init__.py").is_file():
        return extracted
    candidates = [
        child
        for child in extracted.iterdir()
        if child.is_dir() and (child / "app" / "__init__.py").is_file()
    ]
    if len(candidates) != 1:
        raise InstallerError(
            "Release must contain one AI Factory source root with app/__init__.py"
        )
    return candidates[0]


def _version(root: Path) -> str:
    namespace: dict[str, object] = {}
    version_file = root / "app" / "__init__.py"
    for line in version_file.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("__version__") and "=" in line:
            value = line.split("=", 1)[1].strip().strip("\"'")
            if value:
                return value
    raise InstallerError("Release source does not declare __version__")


def _manifest(root: Path) -> tuple[Path | None, dict | None]:
    manifest_path = root / "release.json"
    if not manifest_path.is_file():
        return None, None
    try:
        loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallerError(f"Release manifest is invalid JSON: {exc}") from exc
    if not isinstance(loaded, dict) or loaded.get("format") != 1:
        raise InstallerError("Release manifest has an unsupported format")
    artifacts = loaded.get("artifacts")
    if not isinstance(artifacts, list):
        raise InstallerError("Release manifest has no artifact index")
    listed: set[str] = set()
    for record in artifacts:
        if not isinstance(record, dict):
            raise InstallerError("Release manifest contains an invalid artifact")
        relative = _safe_relative(str(record.get("path", "")))
        relative_name = relative.as_posix()
        if relative_name in {"release.json", "release.json.asc"}:
            raise InstallerError("Release manifest cannot index its own signature")
        if relative_name in listed:
            raise InstallerError(f"Release manifest repeats an artifact: {relative}")
        listed.add(relative_name)
        artifact = root.joinpath(*relative.parts)
        if not artifact.is_file() or artifact.is_symlink():
            raise InstallerError(f"Release artifact is missing: {relative}")
        size = artifact.stat().st_size
        digest = _sha256_file(artifact)
        if size != record.get("size") or digest != record.get("sha256"):
            raise InstallerError(f"Release artifact verification failed: {relative}")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).as_posix()
        not in {"release.json", "release.json.asc"}
    }
    unlisted = sorted(actual - listed)
    if unlisted:
        preview = ", ".join(unlisted[:3])
        raise InstallerError(f"Release contains unlisted artifact(s): {preview}")
    return manifest_path, loaded


def _verify_signature(
    manifest_path: Path | None,
    *,
    keyring: Path,
    runner: CommandRunner,
    required: bool,
) -> bool:
    if manifest_path is None:
        if required:
            raise InstallerError("A signed release manifest is required")
        return False
    signature = manifest_path.with_suffix(manifest_path.suffix + ".asc")
    if not signature.is_file():
        if required:
            raise InstallerError("Release manifest signature is missing")
        return False
    if not keyring.is_file():
        raise InstallerError(f"Release verification keyring is missing: {keyring}")
    runner.run(
        [
            "gpgv",
            "--keyring",
            str(keyring),
            str(signature),
            str(manifest_path),
        ]
    )
    return True


@contextmanager
def prepare_release(
    archive: Path,
    *,
    runner: CommandRunner,
    keyring: Path = Path("/etc/aifactory/release-keyring.gpg"),
    require_signature: bool = True,
) -> Iterator[PreparedRelease]:
    if not archive.is_file():
        raise InstallerError(f"Release archive does not exist: {archive}")
    with tempfile.TemporaryDirectory(prefix="aifactory-release-") as temporary:
        extracted = Path(temporary)
        if zipfile.is_zipfile(archive):
            _extract_zip(archive, extracted)
        elif tarfile.is_tarfile(archive):
            _extract_tar(archive, extracted)
        else:
            raise InstallerError("Release must be a ZIP, tar, tar.gz, or tar.zst archive")
        root = _source_root(extracted)
        manifest_path, manifest = _manifest(root)
        verified = _verify_signature(
            manifest_path,
            keyring=keyring,
            runner=runner,
            required=require_signature,
        )
        release_version = _version(root)
        if manifest and manifest.get("version") != release_version:
            raise InstallerError(
                "Release manifest version does not match app.__version__"
            )
        yield PreparedRelease(root, release_version, manifest, verified)
