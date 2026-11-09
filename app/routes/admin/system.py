"""Admin API: session policy and platform release updates."""

import hashlib
import json
import os
import re
import urllib.parse
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    UploadFile,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.database import get_db
from app.models.session_settings import SessionSettings
from app.schemas.session_settings import SessionSettingsUpdate
from app.session_settings import session_timeout_minutes
from app.models.user import User
from app.config import settings
from app.installer.platform import InstallerError
from app.installer.update_source import (
    CHANNEL_FILENAME,
    LEGACY_CHANNEL_FILENAME,
    parse_channel,
    validate_git_source,
)
from app.release_catalog import semantic_version
from app.update_queue import read_update_status, update_queue_root

router = APIRouter()


@router.get("/session-settings", response_model=SessionSettingsUpdate)
async def get_session_settings(
    admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return {"timeout_minutes": await session_timeout_minutes(db)}


@router.put("/session-settings", response_model=SessionSettingsUpdate)
async def update_session_settings(
    payload: SessionSettingsUpdate,
    admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    record = await db.get(SessionSettings, 1)
    if record is None:
        record = SessionSettings(id=1, timeout_minutes=payload.timeout_minutes)
        db.add(record)
    else:
        record.timeout_minutes = payload.timeout_minutes
    await db.commit()
    return {"timeout_minutes": record.timeout_minutes}


def _github_channel_url(
    repository: str, ref: str, filename: str = CHANNEL_FILENAME
) -> str:
    repository, ref = validate_git_source(repository, ref)
    parsed = urllib.parse.urlparse(repository)
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if parsed.scheme != "https" or parsed.hostname != "github.com" or len(parts) != 2:
        raise InstallerError(
            "Sürüm kontrolü yalnızca HTTPS GitHub repository adresleri için destekleniyor."
        )
    owner, project = parts
    if project.endswith(".git"):
        project = project[:-4]
    if not owner or not project:
        raise InstallerError("GitHub repository adresi geçersiz.")
    return (
        "https://raw.githubusercontent.com/"
        f"{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(project, safe='')}/"
        f"{urllib.parse.quote(ref, safe='/')}/{filename}"
    )


async def _fetch_release_channel(repository: str, ref: str):
    channel_url = _github_channel_url(repository, ref)
    try:
        async with httpx.AsyncClient(timeout=12.0, follow_redirects=True) as client:
            response = await client.get(
                channel_url, headers={"User-Agent": "AIFactory-Controller/1"}
            )
            if response.status_code == 404:
                # Channels written before 3.49.1 only have the devcloud name.
                response = await client.get(
                    _github_channel_url(repository, ref, LEGACY_CHANNEL_FILENAME),
                    headers={"User-Agent": "AIFactory-Controller/1"},
                )
            response.raise_for_status()
            return parse_channel(response.json())
    except (httpx.HTTPError, ValueError, InstallerError) as exc:
        raise InstallerError(
            "Yayın kanalı okunamadı. Repository, branch/tag ve GitHub erişimini kontrol edin."
        ) from exc


@router.get("/system/update-info")
async def get_system_update_info(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Get the current checkout revision and application version."""
    import subprocess
    from app.config import settings

    if not settings.UPDATES_ENABLED:
        return {
            "commit": "image",
            "branch": "container",
            "status": "Container updates are managed by the host installer.",
            "version": settings.APP_VERSION,
            "update_source_type": settings.UPDATE_SOURCE_TYPE,
            "update_source": settings.UPDATE_SOURCE,
            "update_ref": settings.UPDATE_REF,
        }
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=settings.BASE_DIR,
            text=True,
            timeout=5,
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=settings.BASE_DIR,
            text=True,
            timeout=5,
        ).strip()
        return {
            "commit": commit,
            "branch": branch,
            "status": "Hazır",
            "version": settings.APP_VERSION,
            "update_source_type": settings.UPDATE_SOURCE_TYPE,
            "update_source": settings.UPDATE_SOURCE,
            "update_ref": settings.UPDATE_REF,
        }
    except Exception as exc:
        return {
            "commit": "unknown",
            "branch": "unknown",
            "status": str(exc),
            "version": settings.APP_VERSION,
            "update_source_type": settings.UPDATE_SOURCE_TYPE,
            "update_source": settings.UPDATE_SOURCE,
            "update_ref": settings.UPDATE_REF,
        }


@router.get("/system/release-upload/status")
async def get_release_upload_status(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Return the root-owned queued updater's durable status."""
    if not settings.UPDATES_ENABLED:
        raise HTTPException(status_code=503, detail="Release updates are disabled.")
    return read_update_status()


@router.post("/system/release-check")
async def check_git_release_update(
    repository: Annotated[str, Form()],
    ref: Annotated[str, Form()],
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Compare the installed controller version with a GitHub release channel."""
    if not settings.UPDATES_ENABLED:
        raise HTTPException(status_code=503, detail="Release güncellemeleri devre dışı.")
    try:
        repository, ref = validate_git_source(repository, ref)
        channel = await _fetch_release_channel(repository, ref)
    except InstallerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    installed_semantic = semantic_version(settings.APP_VERSION)
    published_semantic = semantic_version(channel.version)
    update_available = settings.APP_VERSION != channel.version
    published_is_older = False
    if installed_semantic is not None and published_semantic is not None:
        update_available = published_semantic > installed_semantic
        published_is_older = published_semantic < installed_semantic
    return {
        "installed_version": settings.APP_VERSION,
        "published_version": channel.version,
        "update_available": update_available,
        "published_is_older": published_is_older,
        "repository": repository,
        "ref": ref,
        "filename": channel.filename,
        "size": channel.size,
    }


@router.post("/system/release-source", status_code=status.HTTP_202_ACCEPTED)
async def queue_git_release_update(
    repository: Annotated[str, Form()],
    ref: Annotated[str, Form()],
    _admin: Annotated[User, Depends(get_current_admin_user)],
    allow_unsigned: Annotated[bool, Form()] = False,
    target_version: Annotated[str, Form()] = "",
):
    """Queue a platform release selected through a Git channel file."""
    if not settings.UPDATES_ENABLED:
        raise HTTPException(status_code=503, detail="Release updates are disabled.")
    try:
        repository, ref = validate_git_source(repository, ref)
    except InstallerError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if target_version and semantic_version(target_version) is None:
        raise HTTPException(status_code=422, detail="Hedef sürüm geçersiz.")
    root = update_queue_root()
    root.mkdir(parents=True, exist_ok=True)
    if (root / "pending.json").exists() or (root / "running.json").exists():
        raise HTTPException(
            status_code=409,
            detail="Another release update is already queued or running.",
        )
    request = {
        "state": "queued",
        "queued_at": datetime.now(timezone.utc).isoformat(),
        "source_type": "git",
        "repository": repository,
        "ref": ref,
        "filename": f"{repository}@{ref}",
        "target_version": target_version or None,
        "allow_unsigned": allow_unsigned,
    }
    marker_tmp = root / "pending.tmp"
    marker_tmp.write_text(json.dumps(request, indent=2) + "\n", encoding="utf-8")
    os.chmod(marker_tmp, 0o600)
    os.replace(marker_tmp, root / "pending.json")
    return {
        "state": "queued",
        "source_type": "git",
        "repository": repository,
        "ref": ref,
        "target_version": target_version or None,
        "allow_unsigned": allow_unsigned,
    }


@router.post(
    "/system/release-upload",
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_release_update(
    release: Annotated[UploadFile, File()],
    _admin: Annotated[User, Depends(get_current_admin_user)],
    allow_unsigned: Annotated[bool, Form()] = False,
):
    """Stage a platform release for the root-owned systemd updater."""
    if not settings.UPDATES_ENABLED:
        raise HTTPException(status_code=503, detail="Release updates are disabled.")
    filename = Path(release.filename or "").name
    if not filename.lower().endswith((".zip", ".tar", ".tar.gz", ".tgz")):
        raise HTTPException(
            status_code=422,
            detail="Release must be a ZIP, tar, tar.gz, or tgz archive.",
        )
    root = update_queue_root()
    uploads = root / "uploads"
    root.mkdir(parents=True, exist_ok=True)
    uploads.mkdir(parents=True, exist_ok=True)
    if (root / "pending.json").exists() or (root / "running.json").exists():
        raise HTTPException(
            status_code=409,
            detail="Another release update is already queued or running.",
        )
    suffix = "".join(Path(filename).suffixes[-2:]) or ".release"
    destination = uploads / f"{uuid.uuid4().hex}{suffix}"
    temporary = destination.with_suffix(destination.suffix + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("xb") as handle:
            while chunk := await release.read(1024 * 1024):
                size += len(chunk)
                if size > settings.UPDATE_MAX_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Release upload exceeds the configured size limit.",
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if not size:
            raise HTTPException(status_code=422, detail="Release archive is empty.")
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        request = {
            "state": "queued",
            "queued_at": datetime.now(timezone.utc).isoformat(),
            "source_type": "bundle",
            "filename": filename,
            "bundle": str(destination),
            "size": size,
            "sha256": digest.hexdigest(),
            "allow_unsigned": allow_unsigned,
        }
        marker_tmp = root / "pending.tmp"
        marker_tmp.write_text(
            json.dumps(request, indent=2) + "\n", encoding="utf-8"
        )
        os.chmod(marker_tmp, 0o600)
        os.replace(marker_tmp, root / "pending.json")
    except Exception:
        temporary.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    return {
        "state": "queued",
        "filename": filename,
        "size": size,
        "sha256": digest.hexdigest(),
        "allow_unsigned": allow_unsigned,
    }


def _checkout_version(project_dir: Path) -> str:
    """Read the version the updater just checked out on disk.

    The running process still holds the pre-update ``app.__version__``.
    """
    version_file = project_dir / "app" / "__init__.py"
    if version_file.is_file():
        content = version_file.read_text(encoding="utf-8")
        match = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', content)
        if match:
            return match.group(1)
    return "3.0.0"


@router.post("/system/update-stream")
async def run_system_update_stream(
    _admin: Annotated[User, Depends(get_current_admin_user)],
):
    """Admin: Run the guarded updater and stream its combined output."""
    import asyncio
    import json
    from pathlib import Path
    from fastapi.responses import StreamingResponse
    from app.config import settings

    if not settings.UPDATES_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Container updates are managed by the host installer.",
        )

    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def emit(text: str, level: str = "info"):
        payload = json.dumps({"type": "log", "level": level, "text": text}, ensure_ascii=False)
        await queue.put(f"data: {payload}\n\n")

    async def run_updater():
        try:
            project_dir = Path(settings.BASE_DIR).resolve()
            update_script = project_dir / "deploy" / "update.sh"
            if not update_script.is_file():
                raise FileNotFoundError(f"Güncelleme betiği bulunamadı: {update_script}")

            await emit("AI Factory platform güncellemesi başlatıldı.", "info")
            proc = await asyncio.create_subprocess_exec(
                "bash",
                str(update_script),
                cwd=str(project_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            if proc.stdout is None:
                raise RuntimeError("Güncelleme çıktısı okunamadı.")

            while True:
                raw_line = await proc.stdout.readline()
                if not raw_line:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    await emit(line, "info")

            return_code = await proc.wait()
            if return_code != 0:
                error_payload = json.dumps(
                    {
                        "type": "error",
                        "text": f"Güncelleme başarısız oldu (çıkış kodu: {return_code}).",
                    },
                    ensure_ascii=False,
                )
                await queue.put(f"data: {error_payload}\n\n")
                return

            commit_proc = await asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "--short",
                "HEAD",
                cwd=str(project_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            commit_stdout, _ = await commit_proc.communicate()
            commit = commit_stdout.decode("utf-8", errors="replace").strip() or "unknown"
            version = _checkout_version(project_dir)
            done_payload = json.dumps(
                {
                    "type": "done",
                    "text": "Güncelleme tamamlandı; servis doğrulanıyor...",
                    "commit": commit,
                    "version": version,
                },
                ensure_ascii=False,
            )
            await queue.put(f"data: {done_payload}\n\n")
        except Exception as exc:
            err_payload = json.dumps(
                {"type": "error", "text": f"Güncelleme hatası: {str(exc)}"},
                ensure_ascii=False,
            )
            await queue.put(f"data: {err_payload}\n\n")
        finally:
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(run_updater())
        while True:
            item = await queue.get()
            if item is None:
                break
            yield item
        await task

    return StreamingResponse(stream(), media_type="text/event-stream")
