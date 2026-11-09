#!/usr/bin/env python3
"""Build one controller-managed update containing controller and worker images.

Workspace images are intentionally excluded. Production hosts load these
prebuilt artifacts and never build application images or contact a Python package
index during an update.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.installer.update_source import write_channel
from deploy.build_release import ReleaseBuildError, run, version
from deploy.package_offline import (
    DEFAULT_PYTHON_VERSIONS,
    PackageError,
    download_wheels,
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def command(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            list(arguments), cwd=root, check=True, text=True, capture_output=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or getattr(exc, "stdout", "")
        raise ReleaseBuildError(
            f"Command failed: {' '.join(arguments)}{': ' + detail.strip() if detail else ''}"
        ) from exc
    return result.stdout.strip()


def _copy_tracked(root: Path, stage: Path) -> None:
    raw = command(root, "git", "ls-files", "-z")
    for item in (entry for entry in raw.split("\0") if entry):
        relative = PurePosixPath(item)
        if relative.is_absolute() or ".." in relative.parts:
            raise ReleaseBuildError(f"Unsafe tracked path: {item}")
        source = root.joinpath(*relative.parts)
        if source.is_symlink() or not source.is_file():
            raise ReleaseBuildError(f"Unsupported tracked path: {item}")
        target = stage.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _image_digest(root: Path, podman: str, image: str) -> str:
    digest = command(root, podman, "image", "inspect", "--format", "{{.Id}}", image)
    # Podman 5 on Rocky/RHEL 10 returns the image ID as 64 bare hex
    # characters, while older releases include the ``sha256:`` prefix.
    if len(digest) == 64 and all(
        character in "0123456789abcdef" for character in digest.lower()
    ):
        digest = f"sha256:{digest.lower()}"
    if not digest.startswith("sha256:") or len(digest) != 71:
        raise ReleaseBuildError(f"Image has no immutable digest: {image}")
    return digest


def _save_image(root: Path, podman: str, image: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    command(
        root,
        podman,
        "save",
        "--format",
        "oci-archive",
        "-o",
        str(target),
        image,
    )
    if not target.is_file() or not target.stat().st_size:
        raise ReleaseBuildError(f"Podman did not export {image}")


def _artifact_index(stage: Path) -> list[dict[str, object]]:
    return [
        {
            "path": path.relative_to(stage).as_posix(),
            "size": path.stat().st_size,
            "sha256": sha256(path),
        }
        for path in sorted(stage.rglob("*"))
        if path.is_file() and path.name not in {"release.json", "release.json.asc"}
    ]


def build(
    root: Path,
    output_dir: Path,
    *,
    podman: str = "podman",
    controller_source: str = "",
    worker_source: str = "",
    signing_key: str = "",
    release_keyring: Path | None = None,
    python_versions: Iterable[str] = DEFAULT_PYTHON_VERSIONS,
    allow_dirty: bool = False,
) -> Path:
    if not allow_dirty and command(root, "git", "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseBuildError("The Git worktree must be clean")
    commit = command(root, "git", "rev-parse", "HEAD")
    release_version = version(root)
    controller_image = f"localhost/aifactory-controller:{release_version}"
    worker_image = f"localhost/aifactory-worker:{release_version}"
    controller_source = controller_source or f"ghcr.io/aydinguven/aifactory:controller-{release_version}"
    worker_source = worker_source or f"ghcr.io/aydinguven/aifactory:worker-{release_version}"
    for image in (controller_image, worker_image):
        command(root, podman, "image", "exists", image)

    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"aifactory-platform-update-v{release_version}-{commit[:12]}.tar.gz"
    with tempfile.TemporaryDirectory(prefix="aifactory-platform-") as temporary:
        stage = Path(temporary) / f"aifactory-{release_version}"
        stage.mkdir()
        _copy_tracked(root, stage)
        try:
            bundled_python_versions = download_wheels(
                root,
                stage / "offline/wheels",
                python_versions,
            )
        except PackageError as exc:
            raise ReleaseBuildError(
                f"Could not bundle native-worker Python dependencies: {exc}"
            ) from exc
        if release_keyring is not None:
            keyring = release_keyring.resolve()
            if not keyring.is_file() or keyring.is_symlink() or not keyring.stat().st_size:
                raise ReleaseBuildError("Release verification keyring is missing or invalid")
            shutil.copy2(keyring, stage / "release-keyring.gpg")
        controller_archive = stage / "offline/controller-images/aifactory-controller.tar"
        worker_archive = stage / "offline/worker-images/aifactory-worker.tar"
        _save_image(root, podman, controller_image, controller_archive)
        _save_image(root, podman, worker_image, worker_archive)
        platform_manifest = {
            "format": 1,
            "version": release_version,
            "source_commit": commit,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "target": {
                "os": "linux",
                "architecture": "amd64",
                "native_worker_python_versions": bundled_python_versions,
            },
            "workspace_images_included": False,
            "images": {
                "controller": {
                    "image": controller_image,
                    "source": controller_source,
                    "digest": _image_digest(root, podman, controller_image),
                    "archive": controller_archive.relative_to(stage).as_posix(),
                    "sha256": sha256(controller_archive),
                    "size": controller_archive.stat().st_size,
                },
                "worker": {
                    "image": worker_image,
                    "source": worker_source,
                    "digest": _image_digest(root, podman, worker_image),
                    "archive": worker_archive.relative_to(stage).as_posix(),
                    "sha256": sha256(worker_archive),
                    "size": worker_archive.stat().st_size,
                },
            },
        }
        (stage / "platform-release.json").write_text(
            json.dumps(platform_manifest, indent=2) + "\n", encoding="utf-8"
        )
        release_manifest = {
            "format": 1,
            "version": release_version,
            "source_commit": commit,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "artifacts": _artifact_index(stage),
        }
        release_path = stage / "release.json"
        release_path.write_text(json.dumps(release_manifest, indent=2) + "\n", encoding="utf-8")
        if signing_key:
            run(
                root,
                "gpg",
                "--batch",
                "--yes",
                "--armor",
                "--local-user",
                signing_key,
                "--detach-sign",
                str(release_path),
            )
        output.unlink(missing_ok=True)
        with tarfile.open(output, "w:gz") as archive:
            archive.add(stage, arcname=stage.name, recursive=True)
    output.with_name(output.name + ".sha256").write_text(
        f"{sha256(output)}  {output.name}\n", encoding="ascii"
    )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="dist/platform")
    parser.add_argument("--podman", default="podman")
    parser.add_argument("--controller-source", default="")
    parser.add_argument("--worker-source", default="")
    parser.add_argument("--signing-key", default="")
    parser.add_argument("--release-keyring", type=Path)
    parser.add_argument(
        "--channel-output",
        default="",
        help="also write aifactory-update-channel.json at this path",
    )
    parser.add_argument(
        "--channel-url",
        default="",
        help="HTTPS, file://, or repository-relative URL stored in the channel",
    )
    parser.add_argument(
        "--python-version",
        action="append",
        default=None,
        help="native-worker CPython major.minor; repeat for multiple targets (default: 3.12)",
    )
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        built = build(
            root,
            Path(args.output_dir).resolve(),
            podman=args.podman,
            controller_source=args.controller_source,
            worker_source=args.worker_source,
            signing_key=args.signing_key,
            release_keyring=args.release_keyring,
            python_versions=args.python_version or DEFAULT_PYTHON_VERSIONS,
            allow_dirty=args.allow_dirty,
        )
        print(built)
        if args.channel_output:
            channel = Path(args.channel_output).resolve()
            channel.parent.mkdir(parents=True, exist_ok=True)
            write_channel(
                built,
                channel,
                url=args.channel_url or built.name,
            )
            print(channel)
        return 0
    except ReleaseBuildError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
