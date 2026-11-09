"""Public, read-only delivery of verified AI Factory air-gap bundles."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth.dependencies import get_current_user_optional
from app.config import settings
from app.models.user import User
from app.static_assets import STATIC_ASSET_VERSION


DOWNLOAD_NAME_PATTERN = re.compile(
    r"^(?:aifactory|devcloud)-(?:worker-)?offline-(?:v?[0-9]+\.[0-9]+\.[0-9]+-[0-9]{8}-)?[0-9a-f]{12}\.tar(?:\.gz)?(?:\.sha256)?$"
)
templates = Jinja2Templates(
    directory=str(Path(__file__).resolve().parent.parent / "templates")
)
templates.env.globals["app_version"] = settings.APP_VERSION
templates.env.globals["static_version"] = STATIC_ASSET_VERSION
download_router = APIRouter(include_in_schema=False)


def _download_root() -> Path:
    return Path(settings.DOWNLOADS_ROOT).expanduser().resolve()


def _require_downloads_enabled() -> Path:
    if not settings.DOWNLOADS_ENABLED:
        raise HTTPException(status_code=404, detail="İndirmeler etkin değil.")
    root = _download_root()
    if not root.is_dir():
        raise HTTPException(status_code=404, detail="Kullanılabilir indirme bulunamadı.")
    return root


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


@download_router.get("/download/", response_class=HTMLResponse)
async def download_index(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
):
    """List published air-gap bundles and their checksum files."""
    root = _require_downloads_enabled()
    bundles = []
    candidates = [
        path
        for path in [*root.glob("aifactory*-offline-*"), *root.glob("devcloud*-offline-*")]
        if path.is_file()
        and not path.is_symlink()
        and not path.name.endswith(".sha256")
        and DOWNLOAD_NAME_PATTERN.fullmatch(path.name)
    ]
    for archive in sorted(
        candidates,
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ):
        checksum = archive.with_name(archive.name + ".sha256")
        checksum_available = checksum.is_file() and not checksum.is_symlink()
        bundles.append(
            {
                "filename": archive.name,
                "bundle_role": (
                    "worker"
                    if archive.name.startswith(("aifactory-worker-", "devcloud-worker-"))
                    else "server"
                ),
                "url": f"/download/{archive.name}",
                "size_display": _format_size(archive.stat().st_size),
                "modified_at": datetime.fromtimestamp(
                    archive.stat().st_mtime,
                    tz=timezone.utc,
                ).isoformat(),
                "checksum_filename": checksum.name if checksum_available else None,
                "checksum_url": f"/download/{checksum.name}" if checksum_available else None,
            }
        )
    return templates.TemplateResponse(
        request=request,
        name="downloads.html",
        context={
            "app_name": settings.APP_NAME,
            "user": current_user,
            "bundles": bundles,
        },
        headers={"Cache-Control": "private, no-store"},
    )


@download_router.get("/download/install-worker.sh")
async def download_worker_bootstrap():
    """Reject the legacy unauthenticated worker installer."""
    raise HTTPException(
        status_code=410,
        detail=(
            "Bu worker kurulum URL'si kaldırıldı. Admin > Worker Node'ları "
            "bölümünden tek kullanımlık kurulum komutu üretin."
        ),
    )


@download_router.api_route("/download/{filename}", methods=["GET", "HEAD"])
async def download_file(filename: str):
    """Stream one allow-listed bundle file with byte-range support."""
    root = _require_downloads_enabled()
    if not DOWNLOAD_NAME_PATTERN.fullmatch(filename):
        raise HTTPException(status_code=404, detail="İndirme bulunamadı.")
    candidate = root / filename
    path = candidate.resolve()
    if candidate.is_symlink() or path.parent != root or not path.is_file():
        raise HTTPException(status_code=404, detail="İndirme bulunamadı.")
    media_type = (
        "text/plain"
        if filename.endswith(".sha256")
        else "application/gzip"
        if filename.endswith(".tar.gz")
        else "application/x-tar"
    )
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
