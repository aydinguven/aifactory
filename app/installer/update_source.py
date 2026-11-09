"""Resolve immutable release bundles from Git channels or local files."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator

from app.installer.platform import CommandRunner, InstallerError
from app.installer.progress import ProgressReporter
from app.platform_release import sha256_file


MAX_UPDATE_BYTES = 32 * 1024 * 1024 * 1024
CHANNEL_FILENAME = "aifactory-update-channel.json"
# Channel name read by releases before 3.49.1; it stays frozen at 3.49.1.
LEGACY_CHANNEL_FILENAME = "devcloud-update-channel.json"


@dataclass(frozen=True, slots=True)
class ReleaseChannel:
    version: str
    filename: str
    url: str
    sha256: str
    size: int


def validate_git_source(location: str, ref: str) -> tuple[str, str]:
    location = location.strip()
    ref = ref.strip()
    if not location or len(location) > 2048 or any(c in location for c in "\r\n\0"):
        raise InstallerError("Git update source requires a safe repository URL or path")
    if location.startswith("-"):
        raise InstallerError("Git repository cannot start with an option prefix")
    parsed = urllib.parse.urlparse(location)
    if parsed.password:
        raise InstallerError("Git repository URL must not contain an embedded password")
    is_scp = bool(re.fullmatch(r"[^@\s]+@[^:\s]+:.+", location))
    is_local = not parsed.scheme and (location.startswith(("/", "./", "../")))
    if parsed.scheme not in {"https", "ssh", "file"} and not is_scp and not is_local:
        raise InstallerError(
            "Git repository must use HTTPS, SSH, file://, or an explicit local path"
        )
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}", ref)
        or ".." in ref
        or "@{" in ref
    ):
        raise InstallerError("Git update ref is invalid")
    return location, ref


def parse_channel(value: object) -> ReleaseChannel:
    """Validate an already decoded update channel document."""
    if not isinstance(value, dict) or value.get("format") != 1:
        raise InstallerError("Update channel format is unsupported")
    version = str(value.get("version") or "")
    bundle = value.get("bundle")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise InstallerError("Update channel version is invalid")
    if not isinstance(bundle, dict):
        raise InstallerError("Update channel has no platform bundle")
    filename = Path(str(bundle.get("filename") or "")).name
    if filename != str(bundle.get("filename") or "") or not filename:
        raise InstallerError("Update channel bundle filename is unsafe")
    checksum = str(bundle.get("sha256") or "")
    size = int(bundle.get("size") or 0)
    url = str(bundle.get("url") or "").strip()
    if not re.fullmatch(r"[0-9a-f]{64}", checksum) or not 0 < size <= MAX_UPDATE_BYTES:
        raise InstallerError("Update channel bundle integrity metadata is invalid")
    if not url:
        raise InstallerError("Update channel bundle URL is missing")
    return ReleaseChannel(version, filename, url, checksum, size)


def read_channel(repository: Path) -> ReleaseChannel:
    path = repository / CHANNEL_FILENAME
    if not path.is_file() or path.is_symlink():
        path = repository / LEGACY_CHANNEL_FILENAME
    if not path.is_file() or path.is_symlink():
        raise InstallerError(f"Update channel file is missing: {CHANNEL_FILENAME}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InstallerError(f"Update channel is invalid JSON: {exc}") from exc
    return parse_channel(value)


def _copy_and_verify(source, target: Path, channel: ReleaseChannel) -> None:
    progress = ProgressReporter.from_environment()
    digest = hashlib.sha256()
    size = 0
    with target.open("xb") as destination:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size == len(chunk) or size % (16 * 1024 * 1024) < len(chunk):
                progress.download(size, channel.size)
            if size > channel.size or size > MAX_UPDATE_BYTES:
                raise InstallerError("Downloaded update exceeds its declared size")
            digest.update(chunk)
            destination.write(chunk)
    progress.download(size, channel.size)
    if size != channel.size or digest.hexdigest() != channel.sha256:
        target.unlink(missing_ok=True)
        raise InstallerError("Downloaded update checksum verification failed")


def _download_channel_bundle(
    repository: Path,
    channel: ReleaseChannel,
    destination: Path,
    *,
    token_file: Path | None,
) -> None:
    parsed = urllib.parse.urlparse(channel.url)
    if parsed.scheme in {"http", "https"}:
        if parsed.scheme != "https":
            raise InstallerError("Remote update bundles require HTTPS")
        headers = {"User-Agent": "AIFactory-Updater/1"}
        if token_file:
            if not token_file.is_file():
                raise InstallerError(f"Update token file does not exist: {token_file}")
            token = token_file.read_text(encoding="utf-8").strip()
            if not token or any(character.isspace() for character in token):
                raise InstallerError("Update token file is empty or invalid")
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(channel.url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                _copy_and_verify(response, destination, channel)
        except OSError as exc:
            destination.unlink(missing_ok=True)
            raise InstallerError(f"Could not download platform update: {exc}") from exc
        return
    if parsed.scheme == "file":
        source_path = Path(urllib.request.url2pathname(parsed.path)).resolve()
    elif not parsed.scheme:
        relative = PurePosixPath(channel.url.replace("\\", "/"))
        if relative.is_absolute() or ".." in relative.parts:
            raise InstallerError("Relative update bundle path is unsafe")
        source_path = repository.joinpath(*relative.parts).resolve()
        if repository.resolve() not in source_path.parents:
            raise InstallerError("Update bundle escapes the repository")
    else:
        raise InstallerError("Update channel bundle URL must use HTTPS or file://")
    if not source_path.is_file() or source_path.is_symlink():
        raise InstallerError(f"Update bundle does not exist: {source_path}")
    with source_path.open("rb") as source:
        _copy_and_verify(source, destination, channel)


@contextmanager
def resolve_update_bundle(
    *,
    source_type: str,
    location: str,
    ref: str,
    runner: CommandRunner,
    token_file: Path | None = None,
) -> Iterator[Path]:
    if source_type == "bundle":
        bundle = Path(location).expanduser().resolve()
        if not bundle.is_file() or bundle.is_symlink():
            raise InstallerError(f"Release archive does not exist: {bundle}")
        yield bundle
        return
    if source_type != "git":
        raise InstallerError(f"Unsupported update source: {source_type}")
    location, ref = validate_git_source(location, ref)
    with tempfile.TemporaryDirectory(prefix="aifactory-update-source-") as temporary:
        root = Path(temporary)
        checkout = root / "repository"
        checkout.mkdir()
        runner.run(["git", "-C", str(checkout), "init", "--quiet"])
        runner.run(
            [
                "git",
                "-C",
                str(checkout),
                "fetch",
                "--quiet",
                "--depth=1",
                "--",
                location,
                ref,
            ]
        )
        runner.run(
            ["git", "-C", str(checkout), "checkout", "--quiet", "--detach", "FETCH_HEAD"]
        )
        channel = read_channel(checkout)
        destination = root / channel.filename
        _download_channel_bundle(
            checkout, channel, destination, token_file=token_file
        )
        yield destination


def write_channel(bundle: Path, output: Path, *, url: str) -> Path:
    """Write a small Git-friendly channel descriptor for a built platform bundle."""
    if not bundle.is_file():
        raise InstallerError(f"Platform bundle does not exist: {bundle}")
    match = re.search(r"-v([0-9]+\.[0-9]+\.[0-9]+)(?:-|\.)", bundle.name)
    if not match:
        raise InstallerError("Cannot determine platform bundle version")
    payload = {
        "format": 1,
        "version": match.group(1),
        "bundle": {
            "filename": bundle.name,
            "url": url,
            "sha256": sha256_file(bundle),
            "size": bundle.stat().st_size,
        },
    }
    output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return output
