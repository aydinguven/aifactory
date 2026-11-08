#!/usr/bin/env python3
"""Build a manifest-indexed, optionally GPG-signed AI Factory source release ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


class ReleaseBuildError(RuntimeError):
    pass


def run(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            list(args), cwd=root, check=True, text=True, capture_output=True
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as exc:
        raise ReleaseBuildError(f"Command failed: {' '.join(args)}") from exc
    return result.stdout.strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version(root: Path) -> str:
    namespace: dict[str, str] = {}
    exec((root / "app" / "__init__.py").read_text(encoding="utf-8"), namespace)
    return str(namespace["__version__"])


def build(
    root: Path,
    output_dir: Path,
    *,
    signing_key: str = "",
    allow_dirty: bool = False,
) -> Path:
    if not allow_dirty and run(root, "git", "status", "--porcelain"):
        raise ReleaseBuildError("The Git worktree must be clean")
    commit = run(root, "git", "rev-parse", "HEAD")
    files = [
        item
        for item in run(root, "git", "ls-files", "-z").split("\0")
        if item
    ]
    release_version = version(root)
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / (
        f"devcloud-release-v{release_version}-{commit[:12]}.zip"
    )
    with tempfile.TemporaryDirectory(prefix="aifactory-release-build-") as temp:
        stage = Path(temp) / f"devcloud-{release_version}"
        stage.mkdir()
        for raw in files:
            relative = PurePosixPath(raw)
            if relative.is_absolute() or ".." in relative.parts:
                raise ReleaseBuildError(f"Unsafe tracked path: {raw}")
            source = root.joinpath(*relative.parts)
            if source.is_symlink() or not source.is_file():
                raise ReleaseBuildError(f"Unsupported tracked path: {raw}")
            target = stage.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
        artifacts = []
        for path in sorted(item for item in stage.rglob("*") if item.is_file()):
            artifacts.append(
                {
                    "path": path.relative_to(stage).as_posix(),
                    "size": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
        manifest = {
            "format": 1,
            "version": release_version,
            "source_commit": commit,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "artifacts": artifacts,
        }
        manifest_path = stage / "release.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
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
                str(manifest_path),
            )
        output.unlink(missing_ok=True)
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(item for item in stage.rglob("*") if item.is_file()):
                archive.write(
                    path,
                    (Path(stage.name) / path.relative_to(stage)).as_posix(),
                )
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{sha256(output)}  {output.name}\n", encoding="ascii"
    )
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default="dist/releases")
    parser.add_argument("--signing-key", default="")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    try:
        output = build(
            root,
            Path(args.output_dir).resolve(),
            signing_key=args.signing_key,
            allow_dirty=args.allow_dirty,
        )
        print(output)
        return 0
    except ReleaseBuildError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
