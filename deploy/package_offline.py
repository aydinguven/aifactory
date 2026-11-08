#!/usr/bin/env python3
"""Build and verify a self-contained AI Factory air-gap bundle.

The builder deliberately packages only files tracked by Git. Generated wheels,
container archives, runtime databases, workspaces, and secrets are never read
from the working tree; fresh artifacts are written to a temporary staging tree.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable


BUNDLE_FORMAT = 4
BUNDLE_ROLES = ("server", "worker")
BUNDLE_PREFIXES = {
    "server": "devcloud-offline",
    "worker": "devcloud-worker-offline",
}
BUNDLE_ROOT_NAMES = {
    "server": "devcloud",
    "worker": "devcloud-worker",
}
DEFAULT_PYTHON_VERSIONS = ("3.12",)
LINUX_PLATFORMS = (
    "manylinux_2_28_x86_64",
    "manylinux2014_x86_64",
)
CONTAINER_PLATFORM = "linux/amd64"
SUPPORTED_SYSTEM_PACKAGE_TARGETS = ("rocky", "rhel")
SUPPORTED_SYSTEM_PACKAGE_MAJORS = ("9", "10")
SYSTEM_REPOSITORY_PACKAGES_FILE = "REQUESTED_PACKAGES"
BOOTSTRAP_SYSTEM_PACKAGES = ("subscription-manager",)
SYSTEM_PACKAGES_BY_BUNDLE_ROLE = {
    "server": {
        "rocky": BOOTSTRAP_SYSTEM_PACKAGES,
        "rhel": BOOTSTRAP_SYSTEM_PACKAGES,
    },
    "worker": {
        "rocky": BOOTSTRAP_SYSTEM_PACKAGES,
        "rhel": BOOTSTRAP_SYSTEM_PACKAGES,
    },
}
# Compatibility for integrations that imported the original server-wide map.
SYSTEM_PACKAGES_BY_DISTRIBUTION = SYSTEM_PACKAGES_BY_BUNDLE_ROLE["server"]
IMAGES = (
    ("devcloud-vscode-kilo", "localhost/devcloud-vscode-kilo:latest", "vscode-kilo"),
    ("devcloud-vscode-cuda", "localhost/devcloud-vscode-cuda:latest", "vscode-cuda"),
    ("devcloud-jupyter-python", "localhost/devcloud-jupyter-python:latest", "jupyter-python"),
    (
        "devcloud-terminal-rocky",
        "localhost/devcloud-terminal-rocky:latest",
        "terminal-rocky",
    ),
)
CONTROLLER_IMAGE_ARCHIVE = "devcloud-controller"
WORKER_IMAGE_ARCHIVE = "devcloud-worker"
POSTGRESQL_IMAGE_ARCHIVE = "devcloud-postgresql-16"
POSTGRESQL_IMAGE = "localhost/aifactory-postgresql:16"
POSTGRESQL_SOURCE_IMAGE = os.getenv(
    "AIFACTORY_POSTGRES_SOURCE_IMAGE",
    "quay.io/sclorg/postgresql-16-c10s:latest",
)
CONTROLLER_SOURCE_IMAGE = os.getenv(
    "AIFACTORY_CONTROLLER_SOURCE_IMAGE",
    "ghcr.io/aydinguven/devcloud:controller-{version}",
)
WORKER_SOURCE_IMAGE = os.getenv(
    "AIFACTORY_WORKER_SOURCE_IMAGE",
    "ghcr.io/aydinguven/devcloud:worker-{version}",
)


class PackageError(RuntimeError):
    """A user-actionable packaging or verification failure."""


def _archive_name_variants(paths: set[str]) -> list[set[str]]:
    """Image archive names of bundles built before and after the aifactory rename."""
    return [paths, {path.replace("/devcloud-", "/aifactory-") for path in paths}]


def run(
    command: list[str],
    *,
    cwd: Path | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a command and stop immediately if it fails."""
    print(f"--> {' '.join(command)}")
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            capture_output=capture_output,
        )
    except FileNotFoundError as exc:
        raise PackageError(f"Required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        suffix = f"\n{details}" if details else ""
        raise PackageError(
            f"Command failed with exit code {exc.returncode}: {' '.join(command)}{suffix}"
        ) from exc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def image_digest(podman_bin: str, image: str) -> str:
    result = run(
        [podman_bin, "image", "inspect", "--format", "{{.Id}}", image],
        capture_output=True,
    )
    value = (result.stdout or "").strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", value):
        value = f"sha256:{value}"
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise PackageError(f"Podman returned an invalid image digest for {image}")
    return value


def write_platform_release(
    bundle_root: Path,
    *,
    version: str,
    source_commit: str,
    controller_digest: str,
    worker_digest: str,
) -> Path:
    images = {}
    for role, digest, archive_path, image, source in (
        (
            "controller",
            controller_digest,
            bundle_root / f"offline/controller-images/{CONTROLLER_IMAGE_ARCHIVE}.tar",
            f"localhost/aifactory-controller:{version}",
            CONTROLLER_SOURCE_IMAGE.format(version=version),
        ),
        (
            "worker",
            worker_digest,
            bundle_root / f"offline/worker-images/{WORKER_IMAGE_ARCHIVE}.tar",
            f"localhost/aifactory-worker:{version}",
            WORKER_SOURCE_IMAGE.format(version=version),
        ),
    ):
        if not archive_path.is_file() or archive_path.is_symlink():
            raise PackageError(f"Platform {role} image archive is missing")
        images[role] = {
            "image": image,
            "source": source,
            "digest": digest,
            "archive": archive_path.relative_to(bundle_root).as_posix(),
            "sha256": sha256_file(archive_path),
            "size": archive_path.stat().st_size,
        }
    path = bundle_root / "platform-release.json"
    path.write_text(
        json.dumps(
            {
                "format": 1,
                "version": version,
                "source_commit": source_commit,
                "workspace_images_included": False,
                "images": images,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def write_release_manifest(
    bundle_root: Path,
    *,
    version: str,
    source_commit: str,
) -> Path:
    artifacts = []
    for path in sorted(item for item in bundle_root.rglob("*") if item.is_file()):
        relative = path.relative_to(bundle_root).as_posix()
        if relative in {"release.json", "release.json.asc"}:
            continue
        artifacts.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    target = bundle_root / "release.json"
    target.write_text(
        json.dumps(
            {
                "format": 1,
                "version": version,
                "source_commit": source_commit,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "artifacts": artifacts,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


def normalize_python_version(version: str) -> tuple[str, str]:
    parts = version.strip().split(".")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        raise PackageError(
            f"Invalid Python version {version!r}; use major.minor, for example 3.12"
        )
    major, minor = parts
    if major != "3" or int(minor) < 11:
        raise PackageError("AI Factory requires a CPython version of 3.11 or newer")
    return f"{major}.{int(minor)}", f"{major}{int(minor)}"


def git_output(root_dir: Path, *arguments: str) -> str:
    result = run(
        ["git", *arguments],
        cwd=root_dir,
        capture_output=True,
    )
    return result.stdout.strip()


def assert_clean_tracked_tree(root_dir: Path) -> None:
    modified = git_output(root_dir, "status", "--porcelain", "--untracked-files=no")
    if modified:
        raise PackageError(
            "Tracked files have uncommitted changes. Commit them before packaging so "
            "the manifest identifies the exact source revision.\n" + modified
        )


def tracked_files(root_dir: Path) -> list[str]:
    output = git_output(root_dir, "ls-files", "-z")
    return [entry for entry in output.split("\0") if entry]


def is_forbidden_tracked_path(relative_path: PurePosixPath) -> bool:
    value = relative_path.as_posix()
    if value in {".env", "data/aifactory.db", "data/devcloud.db"}:
        return True
    if value.startswith("data/workspaces/") and value != "data/workspaces/.gitkeep":
        return True
    if value.startswith("data/test_"):
        return True
    if value.startswith("offline/wheels/") and value != "offline/wheels/.gitkeep":
        return True
    if value.startswith("offline/images/") and value != "offline/images/.gitkeep":
        return True
    if value.startswith("dist/"):
        return True
    return value.endswith((".tar", ".tar.gz"))


def copy_tracked_source(
    root_dir: Path,
    destination: Path,
    files: Iterable[str] | None = None,
) -> None:
    """Copy tracked source files while rejecting unsafe or secret-bearing paths."""
    for raw_path in files if files is not None else tracked_files(root_dir):
        relative = PurePosixPath(raw_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise PackageError(f"Unsafe tracked path: {raw_path}")
        if is_forbidden_tracked_path(relative):
            raise PackageError(
                f"Refusing to package sensitive or generated tracked file: {raw_path}"
            )

        source = root_dir.joinpath(*relative.parts)
        target = destination.joinpath(*relative.parts)
        if source.is_symlink():
            raise PackageError(f"Refusing to package symlink: {raw_path}")
        if not source.is_file():
            raise PackageError(f"Tracked source file is missing: {raw_path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def copy_release_keyring(source: Path, bundle_root: Path) -> Path:
    source = source.resolve()
    if not source.is_file() or source.is_symlink() or not source.stat().st_size:
        raise PackageError("Release verification keyring is missing or invalid")
    target = bundle_root / "release-keyring.gpg"
    shutil.copy2(source, target)
    return target


def download_wheels(
    root_dir: Path,
    wheels_dir: Path,
    python_versions: Iterable[str],
) -> list[str]:
    requirements = root_dir / "requirements.txt"
    normalized_versions: list[str] = []
    wheels_dir.mkdir(parents=True, exist_ok=True)

    for requested_version in python_versions:
        display_version, compact_version = normalize_python_version(requested_version)
        if display_version in normalized_versions:
            continue
        normalized_versions.append(display_version)
        abi = f"cp{compact_version}"
        command = [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--dest",
            str(wheels_dir),
            "--only-binary=:all:",
        ]
        for platform in LINUX_PLATFORMS:
            command.extend(("--platform", platform))
        command.extend(
            (
                "--implementation",
                "cp",
                "--python-version",
                compact_version,
                "--abi",
                abi,
                "--abi",
                "abi3",
                "--abi",
                "none",
                "-r",
                str(requirements),
            )
        )
        print(f"\nDownloading Linux x86_64 wheels for CPython {display_version}...")
        run(command)

    wheels = sorted(wheels_dir.glob("*.whl"))
    if not wheels:
        raise PackageError("pip completed without producing any wheel files")
    return normalized_versions


def read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    if not path.is_file():
        raise PackageError(f"Operating-system metadata not found: {path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def detect_system_package_profile(
    os_release_path: Path = Path("/etc/os-release"),
    *,
    bundle_role: str = "server",
    system_name: str | None = None,
    machine: str | None = None,
) -> dict[str, object]:
    current_system = (system_name or platform.system()).lower()
    current_machine = (machine or platform.machine()).lower()
    if current_system != "linux":
        raise PackageError("System RPMs can only be collected on a Linux build host")
    if current_machine not in {"x86_64", "amd64"}:
        raise PackageError(
            f"System RPM collection requires x86_64, but the build host is {current_machine}"
        )

    release = read_os_release(os_release_path)
    distribution_id = release.get("ID", "").lower()
    version_id = release.get("VERSION_ID", "")
    major_version = version_id.split(".", 1)[0]
    if distribution_id not in SUPPORTED_SYSTEM_PACKAGE_TARGETS:
        raise PackageError(
            "System RPM collection supports Rocky Linux and RHEL 9/10 only; "
            f"this host reports ID={distribution_id or 'unknown'}"
        )
    if major_version not in SUPPORTED_SYSTEM_PACKAGE_MAJORS:
        raise PackageError(
            "System RPM collection supports major versions 9 and 10 only; "
            f"this host reports VERSION_ID={version_id or 'unknown'}"
        )
    if bundle_role not in BUNDLE_ROLES:
        raise PackageError(f"Unsupported bundle role: {bundle_role!r}")

    packages = list(
        SYSTEM_PACKAGES_BY_BUNDLE_ROLE[bundle_role][distribution_id]
    )
    return {
        "distribution_id": distribution_id,
        "version_id": version_id,
        "major_version": major_version,
        "architecture": "x86_64",
        "profile": f"{distribution_id}-{major_version}-x86_64",
        "bundle_role": bundle_role,
        "requested_packages": packages,
    }


def download_system_rpms(
    system_rpms_root: Path,
    *,
    bundle_role: str = "server",
    dnf_bin: str = "dnf",
    createrepo_bin: str = "createrepo_c",
    os_release_path: Path = Path("/etc/os-release"),
    system_name: str | None = None,
    machine: str | None = None,
) -> dict[str, object]:
    profile = detect_system_package_profile(
        os_release_path,
        bundle_role=bundle_role,
        system_name=system_name,
        machine=machine,
    )
    destination = system_rpms_root / str(profile["profile"])
    destination.mkdir(parents=True, exist_ok=True)
    packages = [str(value) for value in profile["requested_packages"]]

    print(
        "\nDownloading the subscription-manager bootstrap RPM closure for "
        f"{profile['profile']}..."
    )
    run(
        [
            dnf_bin,
            "download",
            "--resolve",
            "--alldeps",
            "--arch=x86_64",
            "--arch=noarch",
            "--destdir",
            str(destination),
            *packages,
        ]
    )
    rpms = sorted(destination.glob("*.rpm"))
    if not rpms:
        raise PackageError("DNF completed without producing any system RPM files")
    missing = [
        package
        for package in packages
        if not any(path.name.startswith(f"{package}-") for path in rpms)
    ]
    if missing:
        raise PackageError(
            "The system RPM set is missing requested packages: " + ", ".join(missing)
        )
    requested_packages_path = destination / SYSTEM_REPOSITORY_PACKAGES_FILE
    requested_packages_path.write_text(
        "".join(f"{package}\n" for package in packages),
        encoding="ascii",
    )
    print(f"Creating the offline DNF repository for {profile['profile']}...")
    run([createrepo_bin, "--no-database", str(destination)])
    repository_metadata = destination / "repodata" / "repomd.xml"
    if not repository_metadata.is_file():
        raise PackageError(
            "createrepo completed without producing repodata/repomd.xml"
        )
    repository_files = sorted(
        path for path in destination.rglob("*") if path.is_file()
    )
    checksum_path = system_rpms_root / "SHA256SUMS"
    checksum_path.write_text(
        "".join(
            f"{sha256_file(path)}  {path.relative_to(system_rpms_root).as_posix()}\n"
            for path in repository_files
        ),
        encoding="ascii",
    )
    return profile


def export_images(
    root_dir: Path,
    images_dir: Path,
    *,
    podman_bin: str,
    skip_build: bool,
) -> None:
    run([podman_bin, "--version"], capture_output=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    for archive_name, image_tag, context_name in IMAGES:
        context = root_dir / "containers" / context_name
        if not skip_build:
            print(f"\nBuilding {image_tag}...")
            run(
                [
                    podman_bin,
                    "build",
                    "--platform",
                    CONTAINER_PLATFORM,
                    "-t",
                    image_tag,
                    str(context),
                ]
            )
        else:
            run([podman_bin, "image", "exists", image_tag])

        archive = images_dir / f"{archive_name}.tar"
        print(f"Exporting {image_tag} to {archive.name}...")
        run([podman_bin, "save", "-o", str(archive), image_tag])
        if not archive.is_file() or archive.stat().st_size == 0:
            raise PackageError(f"Podman did not create a valid archive for {image_tag}")


def export_controller_images(
    root_dir: Path,
    images_dir: Path,
    *,
    podman_bin: str,
    skip_build: bool,
    version: str,
) -> None:
    """Export immutable controller runtime images for a registry-free install."""
    run([podman_bin, "--version"], capture_output=True)
    images_dir.mkdir(parents=True, exist_ok=True)
    controller_image = f"localhost/aifactory-controller:{version}"
    if skip_build:
        run([podman_bin, "image", "exists", controller_image])
    else:
        run(
            [
                podman_bin,
                "build",
                "--platform",
                CONTAINER_PLATFORM,
                "--build-arg",
                f"AIFACTORY_VERSION={version}",
                "--file",
                str(root_dir / "containers/aifactory-controller/Containerfile"),
                "--tag",
                controller_image,
                str(root_dir),
            ]
        )
    try:
        run([podman_bin, "image", "exists", POSTGRESQL_IMAGE])
    except PackageError:
        if skip_build:
            raise PackageError(f"Required image is missing: {POSTGRESQL_IMAGE}")
        run([podman_bin, "pull", POSTGRESQL_SOURCE_IMAGE])
        run([podman_bin, "tag", POSTGRESQL_SOURCE_IMAGE, POSTGRESQL_IMAGE])
    for archive_name, image in (
        (CONTROLLER_IMAGE_ARCHIVE, controller_image),
        (POSTGRESQL_IMAGE_ARCHIVE, POSTGRESQL_IMAGE),
    ):
        archive = images_dir / f"{archive_name}.tar"
        print(f"Exporting {image} to {archive.name}...")
        run(
            [
                podman_bin,
                "save",
                "--format",
                "oci-archive",
                "-o",
                str(archive),
                image,
            ]
        )
        if not archive.is_file() or archive.stat().st_size == 0:
            raise PackageError(
                f"Podman did not create a valid controller archive for {image}"
            )


def export_worker_image(
    root_dir: Path,
    images_dir: Path,
    *,
    podman_bin: str,
    skip_build: bool,
    version: str,
) -> None:
    """Export the rootful worker runtime image for a registry-free install."""
    run([podman_bin, "--version"], capture_output=True)
    images_dir.mkdir(parents=True, exist_ok=True)
    worker_image = f"localhost/aifactory-worker:{version}"
    if skip_build:
        run([podman_bin, "image", "exists", worker_image])
    else:
        run(
            [
                podman_bin,
                "build",
                "--platform",
                CONTAINER_PLATFORM,
                "--build-arg",
                f"AIFACTORY_VERSION={version}",
                "--file",
                str(root_dir / "containers/aifactory-worker/Containerfile"),
                "--tag",
                worker_image,
                str(root_dir),
            ]
        )
    archive = images_dir / f"{WORKER_IMAGE_ARCHIVE}.tar"
    print(f"Exporting {worker_image} to {archive.name}...")
    run(
        [
            podman_bin,
            "save",
            "--format",
            "oci-archive",
            "-o",
            str(archive),
            worker_image,
        ]
    )
    if not archive.is_file() or archive.stat().st_size == 0:
        raise PackageError(f"Podman did not create a valid worker archive for {worker_image}")


def artifact_record(bundle_root: Path, path: Path, kind: str) -> dict[str, object]:
    return {
        "path": path.relative_to(bundle_root).as_posix(),
        "kind": kind,
        "size": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def write_manifest(
    bundle_root: Path,
    *,
    git_commit: str,
    python_versions: list[str],
    bundle_role: str = "server",
    system_package_profile: dict[str, object] | None = None,
) -> Path:
    if bundle_role not in BUNDLE_ROLES:
        raise PackageError(f"Unsupported bundle role: {bundle_role!r}")
    wheels = sorted((bundle_root / "offline" / "wheels").glob("*.whl"))
    images = sorted((bundle_root / "offline" / "images").glob("*.tar"))
    controller_images = sorted(
        (bundle_root / "offline" / "controller-images").glob("*.tar")
    )
    worker_images = sorted(
        (bundle_root / "offline" / "worker-images").glob("*.tar")
    )
    # Workspace images are lifecycle-managed by the controller and deliberately
    # excluded from controller and worker base installation bundles.
    expected_images: set[str] = set()
    actual_images = {path.relative_to(bundle_root).as_posix() for path in images}
    if actual_images != expected_images:
        missing = sorted(expected_images - actual_images)
        extra = sorted(actual_images - expected_images)
        raise PackageError(f"Image archive set is incomplete; missing={missing}, extra={extra}")
    expected_controller_images = (
        {
            f"offline/controller-images/{CONTROLLER_IMAGE_ARCHIVE}.tar",
            f"offline/controller-images/{POSTGRESQL_IMAGE_ARCHIVE}.tar",
        }
        if bundle_role == "server"
        else set()
    )
    actual_controller_images = {
        path.relative_to(bundle_root).as_posix() for path in controller_images
    }
    if actual_controller_images != expected_controller_images:
        raise PackageError(
            "Controller image archive set is incomplete; "
            f"missing={sorted(expected_controller_images - actual_controller_images)}, "
            f"extra={sorted(actual_controller_images - expected_controller_images)}"
        )
    expected_worker_images = {
        f"offline/worker-images/{WORKER_IMAGE_ARCHIVE}.tar"
    }
    actual_worker_images = {
        path.relative_to(bundle_root).as_posix() for path in worker_images
    }
    if actual_worker_images != expected_worker_images:
        raise PackageError(
            "Worker image archive set is incomplete; "
            f"missing={sorted(expected_worker_images - actual_worker_images)}, "
            f"extra={sorted(actual_worker_images - expected_worker_images)}"
        )

    requirements = bundle_root / "requirements.txt"
    system_rpm_root = bundle_root / "offline" / "system-rpms"
    system_rpms = sorted(system_rpm_root.rglob("*.rpm"))
    system_rpm_checksums = system_rpm_root / "SHA256SUMS"
    system_rpm_metadata = sorted(
        path
        for path in system_rpm_root.rglob("*")
        if path.is_file()
        and path != system_rpm_checksums
        and path.suffix.lower() != ".rpm"
    )
    if system_package_profile and not system_rpms:
        raise PackageError("The system package profile has no RPM artifacts")
    if system_rpms and not system_package_profile:
        raise PackageError("System RPM artifacts require a target profile")
    if system_package_profile and not system_rpm_checksums.is_file():
        raise PackageError("The system RPM checksum index is missing")
    if system_package_profile:
        profile_root = system_rpm_root / str(system_package_profile["profile"])
        required_metadata = {
            profile_root / SYSTEM_REPOSITORY_PACKAGES_FILE,
            profile_root / "repodata" / "repomd.xml",
        }
        if not required_metadata.issubset(system_rpm_metadata):
            raise PackageError("The offline DNF repository metadata is incomplete")
    target = {
        "os": "linux",
        "architecture": "x86_64",
        "container_platform": CONTAINER_PLATFORM,
        "python_versions": python_versions,
    }
    if system_package_profile:
        target["system_packages"] = system_package_profile
    manifest = {
        "bundle_format": BUNDLE_FORMAT,
        "bundle_role": bundle_role,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": git_commit,
        "target": target,
        "requirements_sha256": sha256_file(requirements),
        "artifacts": [
            *(artifact_record(bundle_root, path, "python-wheel") for path in wheels),
            *(artifact_record(bundle_root, path, "container-image") for path in images),
            *(
                artifact_record(bundle_root, path, "controller-container-image")
                for path in controller_images
            ),
            *(
                artifact_record(bundle_root, path, "worker-container-image")
                for path in worker_images
            ),
            *(artifact_record(bundle_root, path, "system-rpm") for path in system_rpms),
            *(
                artifact_record(bundle_root, path, "system-rpm-repository-metadata")
                for path in system_rpm_metadata
            ),
            *(
                [artifact_record(bundle_root, system_rpm_checksums, "system-rpm-checksums")]
                if system_package_profile
                else []
            ),
        ],
    }
    manifest_path = bundle_root / "offline" / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def verify_staged_bundle(
    bundle_root: Path,
    *,
    check_runtime: bool = False,
    expected_role: str | None = None,
) -> dict[str, object]:
    manifest_path = bundle_root / "offline" / "MANIFEST.json"
    if not manifest_path.is_file():
        raise PackageError(f"Bundle manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"Cannot read bundle manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PackageError("The bundle manifest must be a JSON object")

    if manifest.get("bundle_format") != BUNDLE_FORMAT:
        raise PackageError(f"Unsupported bundle format: {manifest.get('bundle_format')!r}")
    bundle_role = manifest.get("bundle_role", "server")
    if bundle_role not in BUNDLE_ROLES:
        raise PackageError(f"Unsupported bundle role: {bundle_role!r}")
    if expected_role and bundle_role != expected_role:
        raise PackageError(
            f"This is a {bundle_role} bundle, but a {expected_role} bundle was expected"
        )
    source_commit = manifest.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit.lower())
    ):
        raise PackageError("The manifest contains an invalid source commit")
    target = manifest.get("target")
    if not isinstance(target, dict):
        raise PackageError("The manifest contains no target definition")
    if (
        target.get("os") != "linux"
        or target.get("architecture") != "x86_64"
        or target.get("container_platform") != CONTAINER_PLATFORM
    ):
        raise PackageError("The manifest contains an unsupported target definition")
    python_versions = target.get("python_versions")
    if (
        not isinstance(python_versions, list)
        or not python_versions
        or not all(isinstance(version, str) for version in python_versions)
    ):
        raise PackageError("The manifest contains invalid target Python versions")
    system_packages = target.get("system_packages")
    if system_packages is not None:
        if not isinstance(system_packages, dict):
            raise PackageError("The manifest contains an invalid system package profile")
        distribution_id = system_packages.get("distribution_id")
        major_version = system_packages.get("major_version")
        profile_name = system_packages.get("profile")
        requested_packages = system_packages.get("requested_packages")
        expected_profile = f"{distribution_id}-{major_version}-x86_64"
        if (
            distribution_id not in SUPPORTED_SYSTEM_PACKAGE_TARGETS
            or major_version not in SUPPORTED_SYSTEM_PACKAGE_MAJORS
            or system_packages.get("architecture") != "x86_64"
            or profile_name != expected_profile
            or not isinstance(requested_packages, list)
            or not all(isinstance(package, str) for package in requested_packages)
        ):
            raise PackageError("The manifest contains an unsupported system package profile")
        profile_bundle_role = system_packages.get("bundle_role", bundle_role)
        if profile_bundle_role != bundle_role:
            raise PackageError(
                "The system package profile does not match the bundle role"
            )
        required_packages = set(
            SYSTEM_PACKAGES_BY_BUNDLE_ROLE[bundle_role][str(distribution_id)]
        )
        if not required_packages.issubset(requested_packages):
            raise PackageError("The system package profile omits required packages")
    if check_runtime:
        current_python = f"{sys.version_info.major}.{sys.version_info.minor}"
        if platform.system().lower() != "linux":
            raise PackageError("This bundle targets Linux, but the current host is not Linux")
        if platform.machine().lower() not in {"x86_64", "amd64"}:
            raise PackageError(
                f"This bundle targets x86_64, but the current host is {platform.machine()}"
            )
        if current_python not in python_versions:
            raise PackageError(
                f"This bundle targets CPython {', '.join(python_versions)}, "
                f"but the current host uses {current_python}"
            )
        if system_packages:
            current_profile = detect_system_package_profile()
            if (
                current_profile["distribution_id"]
                != system_packages["distribution_id"]
                or current_profile["major_version"] != system_packages["major_version"]
            ):
                raise PackageError(
                    "This bundle contains system RPMs for "
                    f"{system_packages['profile']}, but this host is "
                    f"{current_profile['profile']}"
                )
    requirements = bundle_root / "requirements.txt"
    if not requirements.is_file():
        raise PackageError("requirements.txt is missing from the bundle")
    if sha256_file(requirements) != manifest.get("requirements_sha256"):
        raise PackageError("requirements.txt checksum does not match the manifest")

    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise PackageError("The manifest contains no artifacts")
    seen_paths: set[str] = set()
    image_paths: set[str] = set()
    controller_image_paths: set[str] = set()
    worker_image_paths: set[str] = set()
    wheel_paths: set[str] = set()
    system_rpm_paths: set[str] = set()
    system_rpm_metadata_paths: set[str] = set()
    system_rpm_checksum_paths: set[str] = set()
    for record in artifacts:
        if not isinstance(record, dict):
            raise PackageError("The manifest contains an invalid artifact record")
        raw_path = record.get("path")
        if not isinstance(raw_path, str):
            raise PackageError("An artifact path is missing from the manifest")
        relative = PurePosixPath(raw_path)
        if relative.is_absolute() or ".." in relative.parts or raw_path in seen_paths:
            raise PackageError(f"Unsafe or duplicate manifest path: {raw_path}")
        seen_paths.add(raw_path)
        artifact = bundle_root.joinpath(*relative.parts)
        if artifact.is_symlink() or not artifact.is_file():
            raise PackageError(f"Bundle artifact is missing: {raw_path}")
        if artifact.stat().st_size != record.get("size"):
            raise PackageError(f"Bundle artifact size does not match: {raw_path}")
        if sha256_file(artifact) != record.get("sha256"):
            raise PackageError(f"Bundle artifact checksum does not match: {raw_path}")
        if record.get("kind") == "container-image":
            image_paths.add(raw_path)
        elif record.get("kind") == "controller-container-image":
            controller_image_paths.add(raw_path)
        elif record.get("kind") == "worker-container-image":
            worker_image_paths.add(raw_path)
        elif record.get("kind") == "python-wheel":
            wheel_paths.add(raw_path)
        elif record.get("kind") == "system-rpm":
            system_rpm_paths.add(raw_path)
        elif record.get("kind") == "system-rpm-repository-metadata":
            system_rpm_metadata_paths.add(raw_path)
        elif record.get("kind") == "system-rpm-checksums":
            system_rpm_checksum_paths.add(raw_path)
        else:
            raise PackageError(f"Unknown artifact kind for {raw_path}")

    expected_images: set[str] = set()
    if image_paths != expected_images:
        raise PackageError("Base bundles must not contain workspace image artifacts")
    expected_controller_images = (
        {
            f"offline/controller-images/{CONTROLLER_IMAGE_ARCHIVE}.tar",
            f"offline/controller-images/{POSTGRESQL_IMAGE_ARCHIVE}.tar",
        }
        if bundle_role == "server"
        else set()
    )
    if controller_image_paths not in _archive_name_variants(expected_controller_images):
        raise PackageError(
            "The manifest does not contain the required controller images"
        )
    expected_worker_images = {
        f"offline/worker-images/{WORKER_IMAGE_ARCHIVE}.tar"
    }
    if worker_image_paths not in _archive_name_variants(expected_worker_images):
        raise PackageError("The manifest does not contain the required worker image")
    if not wheel_paths:
        raise PackageError("The manifest does not contain Python wheels")
    actual_images = {
        path.relative_to(bundle_root).as_posix()
        for pattern in ("*.tar", "*.tar.gz")
        for path in (bundle_root / "offline" / "images").glob(pattern)
    }
    actual_controller_images = {
        path.relative_to(bundle_root).as_posix()
        for path in (bundle_root / "offline" / "controller-images").glob("*.tar")
    }
    actual_worker_images = {
        path.relative_to(bundle_root).as_posix()
        for path in (bundle_root / "offline" / "worker-images").glob("*.tar")
    }
    if actual_controller_images != controller_image_paths:
        raise PackageError("The bundle contains unlisted controller image archives")
    if actual_worker_images != worker_image_paths:
        raise PackageError("The bundle contains unlisted worker image archives")
    actual_wheels = {
        path.relative_to(bundle_root).as_posix()
        for path in (bundle_root / "offline" / "wheels").glob("*.whl")
    }
    actual_system_rpms = {
        path.relative_to(bundle_root).as_posix()
        for path in (bundle_root / "offline" / "system-rpms").rglob("*.rpm")
    }
    system_rpm_root = bundle_root / "offline" / "system-rpms"
    system_rpm_checksum_path = system_rpm_root / "SHA256SUMS"
    actual_system_rpm_metadata = {
        path.relative_to(bundle_root).as_posix()
        for path in system_rpm_root.rglob("*")
        if path.is_file()
        and path != system_rpm_checksum_path
        and path.suffix.lower() != ".rpm"
    }
    if actual_images != image_paths:
        raise PackageError("The image directory contains unlisted or missing artifacts")
    if actual_wheels != wheel_paths:
        raise PackageError("The wheel directory contains unlisted or missing artifacts")
    if actual_system_rpms != system_rpm_paths:
        raise PackageError("The system RPM directory contains unlisted or missing artifacts")
    if actual_system_rpm_metadata != system_rpm_metadata_paths:
        raise PackageError(
            "The system RPM repository contains unlisted or missing metadata"
        )
    if system_packages:
        expected_prefix = f"offline/system-rpms/{system_packages['profile']}/"
        if not system_rpm_paths or not all(
            path.startswith(expected_prefix) for path in system_rpm_paths
        ):
            raise PackageError("The manifest system RPM set does not match its target profile")
        if system_rpm_checksum_paths != {"offline/system-rpms/SHA256SUMS"}:
            raise PackageError("The manifest system RPM checksum index is missing")
        required_metadata_paths = {
            f"{expected_prefix}{SYSTEM_REPOSITORY_PACKAGES_FILE}",
            f"{expected_prefix}repodata/repomd.xml",
        }
        if not required_metadata_paths.issubset(system_rpm_metadata_paths):
            raise PackageError("The manifest offline DNF repository is incomplete")
    elif system_rpm_paths:
        raise PackageError("The manifest lists system RPMs without a target profile")
    elif system_rpm_checksum_paths or system_rpm_metadata_paths:
        raise PackageError("The manifest lists an RPM repository without RPMs")
    return manifest


def create_archive(bundle_root: Path, output_path: Path, *, arcname: str = "devcloud") -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pigz = shutil.which("pigz")
    if pigz is None:
        print("pigz not found; compressing with the standard single-threaded gzip writer...")
        with tarfile.open(output_path, "w:gz") as archive:
            archive.add(bundle_root, arcname=arcname)
        return

    print("Compressing bundle with parallel pigz...")
    with output_path.open("wb") as output_file:
        compressor = subprocess.Popen(
            [pigz, "-c"],
            stdin=subprocess.PIPE,
            stdout=output_file,
        )
        if compressor.stdin is None:
            compressor.terminate()
            compressor.wait()
            raise PackageError("Could not open the pigz input stream")
        try:
            with tarfile.open(fileobj=compressor.stdin, mode="w|") as archive:
                archive.add(bundle_root, arcname=arcname)
            compressor.stdin.close()
            return_code = compressor.wait()
        except BaseException:
            if not compressor.stdin.closed:
                compressor.stdin.close()
            compressor.terminate()
            compressor.wait()
            output_path.unlink(missing_ok=True)
            raise
    if return_code != 0:
        output_path.unlink(missing_ok=True)
        raise PackageError(f"pigz failed with exit code {return_code}")


def write_outer_checksum(bundle_path: Path) -> Path:
    checksum_path = bundle_path.with_name(bundle_path.name + ".sha256")
    checksum_path.write_text(
        f"{sha256_file(bundle_path)}  {bundle_path.name}\n",
        encoding="ascii",
    )
    return checksum_path


def get_app_version(root_dir: Path) -> str:
    version_file = root_dir / "app" / "__init__.py"
    if version_file.is_file():
        content = version_file.read_text(encoding="utf-8")
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
        if match:
            return match.group(1)
    return "3.0.0"


def build_bundle(args: argparse.Namespace) -> tuple[Path, Path]:
    root_dir = Path(__file__).resolve().parent.parent
    assert_clean_tracked_tree(root_dir)
    commit = git_output(root_dir, "rev-parse", "HEAD")
    short_commit = commit[:12]
    version = get_app_version(root_dir)
    bundle_role = args.bundle_role
    bundle_prefix = BUNDLE_PREFIXES[bundle_role]
    bundle_root_name = BUNDLE_ROOT_NAMES[bundle_role]
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    output_dir = Path(args.output_dir).resolve() if args.output_dir else root_dir / "dist"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{bundle_prefix}-v{version}-{today_str}-{short_commit}.tar.gz"
    checksum_path = output_path.with_name(output_path.name + ".sha256")

    # Clean old offline packages to preserve disk
    if getattr(args, "clean_old", True):
        for old_file in output_dir.glob(f"{bundle_prefix}-*"):
            if old_file.is_file() and old_file not in (output_path, checksum_path):
                try:
                    old_file.unlink()
                    print(f"Removed older bundle to preserve disk: {old_file.name}")
                except OSError:
                    pass

    output_path.unlink(missing_ok=True)
    checksum_path.unlink(missing_ok=True)

    print("=" * 78)
    print(f"Building AI Factory {bundle_role} air-gap bundle from Git commit {commit}")
    print("=" * 78)

    with tempfile.TemporaryDirectory(prefix="aifactory-airgap-") as temp_dir:
        bundle_root = Path(temp_dir) / bundle_root_name
        copy_tracked_source(root_dir, bundle_root)
        if args.release_keyring:
            copy_release_keyring(Path(args.release_keyring), bundle_root)
        wheels_dir = bundle_root / "offline" / "wheels"
        controller_images_dir = bundle_root / "offline" / "controller-images"
        worker_images_dir = bundle_root / "offline" / "worker-images"
        system_rpms_dir = bundle_root / "offline" / "system-rpms"

        versions = download_wheels(root_dir, wheels_dir, args.python_version)
        system_package_profile = None
        if not args.skip_system_rpms:
            system_package_profile = download_system_rpms(
                system_rpms_dir,
                bundle_role=bundle_role,
                dnf_bin=args.dnf_bin,
                createrepo_bin=args.createrepo_bin,
            )
        if bundle_role == "server":
            export_controller_images(
                root_dir,
                controller_images_dir,
                podman_bin=args.podman_bin,
                skip_build=args.skip_image_build,
                version=version,
            )
        export_worker_image(
            root_dir,
            worker_images_dir,
            podman_bin=args.podman_bin,
            skip_build=args.skip_image_build,
            version=version,
        )
        write_manifest(
            bundle_root,
            git_commit=commit,
            python_versions=versions,
            bundle_role=bundle_role,
            system_package_profile=system_package_profile,
        )
        if bundle_role == "server":
            write_platform_release(
                bundle_root,
                version=version,
                source_commit=commit,
                controller_digest=image_digest(
                    args.podman_bin, f"localhost/aifactory-controller:{version}"
                ),
                worker_digest=image_digest(
                    args.podman_bin, f"localhost/aifactory-worker:{version}"
                ),
            )
            write_release_manifest(
                bundle_root,
                version=version,
                source_commit=commit,
            )
        print("\nVerifying staged artifacts...")
        verify_staged_bundle(bundle_root, expected_role=bundle_role)
        print(f"Creating {output_path.name}...")
        create_archive(bundle_root, output_path, arcname=bundle_root_name)

    checksum_path = write_outer_checksum(output_path)
    print("=" * 78)
    print(f"Bundle:   {output_path}")
    print(f"Checksum: {checksum_path}")
    print("Upload both files to a Git release; do not commit the generated archive.")
    print("=" * 78)
    return output_path, checksum_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify",
        metavar="EXTRACTED_BUNDLE_DIR",
        help="verify an extracted bundle instead of building one",
    )
    parser.add_argument(
        "--check-runtime",
        action="store_true",
        help="with --verify, require this host to match the bundle target",
    )
    parser.add_argument(
        "--bundle-role",
        choices=BUNDLE_ROLES,
        default="server",
        help="bundle role to build (default: server)",
    )
    parser.add_argument(
        "--expected-role",
        choices=BUNDLE_ROLES,
        help="with --verify, require the manifest to have this bundle role",
    )
    parser.add_argument(
        "--python-version",
        action="append",
        default=None,
        help="target CPython major.minor; repeat for multiple targets (default: 3.12)",
    )
    parser.add_argument("--output-dir", help="output directory (default: ./dist)")
    parser.add_argument(
        "--release-keyring",
        type=Path,
        help="public GPG keyring to embed for subsequent signed updates",
    )
    parser.add_argument(
        "--podman-bin",
        default=os.getenv("PODMAN_BIN", "podman"),
        help="Podman executable path (default: PODMAN_BIN or podman)",
    )
    parser.add_argument(
        "--dnf-bin",
        default=os.getenv("DNF_BIN", "dnf"),
        help="DNF executable used to collect Rocky/RHEL RPMs (default: DNF_BIN or dnf)",
    )
    parser.add_argument(
        "--createrepo-bin",
        default=os.getenv("CREATEREPO_BIN", "createrepo_c"),
        help=(
            "createrepo executable used to build the bundled local repository "
            "(default: CREATEREPO_BIN or createrepo_c)"
        ),
    )
    parser.add_argument(
        "--skip-system-rpms",
        action="store_true",
        help="build a legacy bundle without Rocky/RHEL bootstrap RPMs",
    )
    parser.add_argument(
        "--skip-image-build",
        action="store_true",
        help="export existing controller/PostgreSQL image tags instead of rebuilding them",
    )
    args = parser.parse_args(argv)
    args.python_version = args.python_version or list(DEFAULT_PYTHON_VERSIONS)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.verify:
            root = Path(args.verify).resolve()
            manifest = verify_staged_bundle(
                root,
                check_runtime=args.check_runtime,
                expected_role=args.expected_role,
            )
            print(
                "Air-gap bundle verified: "
                f"role {manifest.get('bundle_role', 'server')}, "
                f"commit {manifest['source_commit']}, "
                f"{len(manifest['artifacts'])} artifacts"
            )
        else:
            build_bundle(args)
        return 0
    except PackageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
