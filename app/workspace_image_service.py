"""Normalize registry and uploaded workspace images into managed OCI archives."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from app.config import settings


class WorkspaceImageError(RuntimeError):
    pass


def image_storage_root() -> Path:
    return Path(settings.WORKSPACE_IMAGES_ROOT).resolve()


def image_archive_path(filename: str) -> Path:
    if not filename or Path(filename).name != filename:
        raise WorkspaceImageError("Invalid workspace image filename")
    root = image_storage_root()
    path = (root / filename).resolve()
    if path.parent != root:
        raise WorkspaceImageError("Workspace image path escapes its storage root")
    return path


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_skopeo(
    arguments: list[str],
    *,
    environment: dict[str, str] | None = None,
    timeout: int | None = None,
) -> str:
    command = [settings.SKOPEO_BIN, *arguments]
    try:
        result = subprocess.run(
            command,
            check=True,
            text=True,
            capture_output=True,
            timeout=timeout or settings.WORKSPACE_IMAGE_IMPORT_TIMEOUT_SECONDS,
            env={**os.environ, **(environment or {})},
        )
    except FileNotFoundError as exc:
        raise WorkspaceImageError(
            "skopeo is not installed in the controller runtime"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceImageError("Workspace image import timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise WorkspaceImageError(detail or "skopeo could not import the image") from exc
    return result.stdout


def _write_registry_authfile(ref: str, username: str, password: str) -> Path | None:
    """Write a 0600 containers auth file for the registry of ``ref``."""
    if not username and not password:
        return None
    if not username or not password:
        raise WorkspaceImageError("Registry username and password must be supplied together")
    plain_ref = ref.removeprefix("docker://")
    registry = plain_ref.split("/", 1)[0]
    if not registry or registry in {".", ".."}:
        raise WorkspaceImageError("Registry reference has no valid registry host")
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    handle = tempfile.NamedTemporaryFile(
        mode="w", prefix="aifactory-registry-auth-", suffix=".json", delete=False
    )
    auth_path = Path(handle.name)
    try:
        json.dump({"auths": {registry: {"auth": encoded}}}, handle)
        handle.close()
        os.chmod(auth_path, 0o600)
    except Exception:
        handle.close()
        auth_path.unlink(missing_ok=True)
        raise
    return auth_path


def _registry_auth_environment(
    source_ref: str, username: str, password: str
) -> tuple[dict[str, str], Path | None]:
    auth_path = _write_registry_authfile(source_ref, username, password)
    if auth_path is None:
        return {}, None
    return {"REGISTRY_AUTH_FILE": str(auth_path)}, auth_path


def _docker_ref(ref: str) -> str:
    normalized = ref.strip()
    if not normalized:
        raise WorkspaceImageError("Registry image reference is required")
    if "://" in normalized and not normalized.startswith("docker://"):
        raise WorkspaceImageError("Only docker:// registry references are supported")
    return normalized if normalized.startswith("docker://") else f"docker://{normalized}"


_MISSING_IMAGE_MARKERS = ("manifest unknown", "name unknown", "not found")
_REGISTRY_INSPECT_TIMEOUT_SECONDS = 120


def registry_tag_digest(
    *,
    image_ref: str,
    username: str = "",
    password: str = "",
    tls_verify: bool = True,
) -> str | None:
    """Return the manifest digest of a registry tag, or None when it does not exist."""
    source = _docker_ref(image_ref)
    arguments = ["inspect"]
    if not tls_verify:
        arguments.append("--tls-verify=false")
    arguments.append(source)
    environment, auth_path = _registry_auth_environment(source, username, password)
    try:
        output = _run_skopeo(
            arguments,
            environment=environment,
            timeout=_REGISTRY_INSPECT_TIMEOUT_SECONDS,
        )
    except WorkspaceImageError as exc:
        if any(marker in str(exc).lower() for marker in _MISSING_IMAGE_MARKERS):
            return None
        raise
    finally:
        if auth_path:
            auth_path.unlink(missing_ok=True)
    try:
        digest = str(json.loads(output).get("Digest") or "")
    except (ValueError, AttributeError) as exc:
        raise WorkspaceImageError("skopeo returned invalid image metadata") from exc
    if not digest:
        raise WorkspaceImageError("skopeo did not report an image digest")
    return digest


def copy_registry_image(
    *,
    source_ref: str,
    destination_ref: str,
    source_username: str = "",
    source_password: str = "",
    destination_username: str = "",
    destination_password: str = "",
    destination_tls_verify: bool = True,
    manifest_format: str = "",
) -> None:
    """Copy one tag between registries, each with its own ephemeral auth file."""
    source = _docker_ref(source_ref)
    destination = _docker_ref(destination_ref)
    if manifest_format not in {"", "v2s2"}:
        raise WorkspaceImageError("Unsupported manifest format")
    auth_paths: list[Path] = []
    try:
        arguments = ["copy", "--remove-signatures"]
        source_auth = _write_registry_authfile(source, source_username, source_password)
        if source_auth:
            auth_paths.append(source_auth)
            arguments += ["--src-authfile", str(source_auth)]
        destination_auth = _write_registry_authfile(
            destination, destination_username, destination_password
        )
        if destination_auth:
            auth_paths.append(destination_auth)
            arguments += ["--dest-authfile", str(destination_auth)]
        if not destination_tls_verify:
            arguments.append("--dest-tls-verify=false")
        if manifest_format:
            arguments += ["--format", manifest_format]
        _run_skopeo([*arguments, source, destination])
    finally:
        for path in auth_paths:
            path.unlink(missing_ok=True)


def _inspect_archive(path: Path) -> dict:
    output = _run_skopeo(["inspect", f"oci-archive:{path}"])
    try:
        metadata = json.loads(output)
    except ValueError as exc:
        raise WorkspaceImageError("skopeo returned invalid image metadata") from exc
    if str(metadata.get("Os") or "linux").lower() != "linux":
        raise WorkspaceImageError("Only Linux workspace images are supported")
    architecture = str(metadata.get("Architecture") or "").lower()
    if architecture not in {"amd64", "x86_64"}:
        raise WorkspaceImageError(
            f"Only linux/amd64 workspace images are supported; received {architecture or 'unknown'}"
        )
    return metadata


def _normalize(
    *,
    source: str,
    image_ref: str,
    environment: dict[str, str] | None = None,
) -> dict[str, object]:
    root = image_storage_root()
    root.mkdir(parents=True, exist_ok=True)
    image_id = str(uuid.uuid4())
    filename = f"{image_id}.tar"
    destination = image_archive_path(filename)
    temporary = root / f".{image_id}.partial"
    try:
        _run_skopeo(
            [
                "copy",
                "--remove-signatures",
                "--override-os",
                "linux",
                "--override-arch",
                "amd64",
                source,
                f"oci-archive:{temporary}:{image_ref}",
            ],
            environment=environment,
        )
        metadata = _inspect_archive(temporary)
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return {
        "id": image_id,
        "image_ref": image_ref,
        "digest": str(metadata.get("Digest") or ""),
        "sha256": sha256_file(destination),
        "filename": filename,
        "size": destination.stat().st_size,
        "architecture": "amd64",
    }


def import_registry_image(
    *,
    image_ref: str,
    source_ref: str,
    username: str = "",
    password: str = "",
) -> dict[str, object]:
    normalized = source_ref.strip()
    if not normalized:
        raise WorkspaceImageError("Registry image reference is required")
    if "://" in normalized and not normalized.startswith("docker://"):
        raise WorkspaceImageError("Only docker:// registry references are supported")
    source = normalized if normalized.startswith("docker://") else f"docker://{normalized}"
    environment, auth_path = _registry_auth_environment(source, username, password)
    try:
        return _normalize(
            source=source,
            image_ref=image_ref,
            environment=environment,
        )
    finally:
        if auth_path:
            auth_path.unlink(missing_ok=True)


def import_uploaded_archive(*, image_ref: str, upload_path: Path) -> dict[str, object]:
    errors: list[str] = []
    for transport in ("oci-archive", "docker-archive"):
        try:
            return _normalize(
                source=f"{transport}:{upload_path}",
                image_ref=image_ref,
            )
        except WorkspaceImageError as exc:
            errors.append(str(exc))
    raise WorkspaceImageError(
        "Uploaded file is not a supported OCI or Docker image archive: "
        + "; ".join(errors[-2:])
    )



def delete_registry_image(
    *,
    source_ref: str,
    username: str = "",
    password: str = "",
) -> None:
    """Delete a managed registry tag using an ephemeral authentication file."""
    normalized = source_ref.strip()
    if not normalized:
        raise WorkspaceImageError("Registry image reference is required")
    if "://" in normalized and not normalized.startswith("docker://"):
        raise WorkspaceImageError("Only docker:// registry references are supported")
    source = normalized if normalized.startswith("docker://") else f"docker://{normalized}"
    environment, auth_path = _registry_auth_environment(source, username, password)
    try:
        try:
            _run_skopeo(["delete", source], environment=environment)
        except WorkspaceImageError as exc:
            detail = str(exc).lower()
            if not any(
                marker in detail
                for marker in ("manifest unknown", "name unknown", "not found")
            ):
                raise
    finally:
        if auth_path:
            auth_path.unlink(missing_ok=True)
