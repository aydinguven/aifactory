from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


RELEASE_PATTERN = re.compile(
    r"^(?:aifactory|devcloud)-(?:release|platform-update)-v"
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"
    r"(?:-(?P<revision>[0-9a-f]{7,40}))?"
    r"(?P<extension>\.zip|\.tar\.gz)$"
)


@dataclass(frozen=True, slots=True)
class PublishedRelease:
    path: Path
    version: str
    sha256: str
    size: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def semantic_version(value: str) -> tuple[int, int, int] | None:
    """Parse the strict release version used by controller and worker bundles."""
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value):
        return None
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def latest_release(downloads_root: Path) -> PublishedRelease | None:
    root = (downloads_root / "releases").resolve()
    if not root.is_dir():
        return None
    candidates = [
        path
        for path in root.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and RELEASE_PATTERN.fullmatch(path.name)
    ]
    if not candidates:
        return None
    def release_key(item: Path) -> tuple[tuple[int, int, int], int, str]:
        match = RELEASE_PATTERN.fullmatch(item.name)
        assert match is not None
        semantic = semantic_version(match.group("version"))
        assert semantic is not None
        return semantic, item.stat().st_mtime_ns, item.name

    path = max(candidates, key=release_key)
    match = RELEASE_PATTERN.fullmatch(path.name)
    assert match is not None
    return PublishedRelease(
        path=path,
        version=match.group("version"),
        sha256=sha256_file(path),
        size=path.stat().st_size,
    )
