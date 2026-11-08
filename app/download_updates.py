"""Admin-triggered builder and publisher for air-gap download bundles."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import settings


logger = logging.getLogger(__name__)

BUNDLE_ROLES = ("server", "worker")
BUNDLE_PREFIXES = {
    "server": "devcloud-offline",
    "worker": "devcloud-worker-offline",
}
# Bundles are still built with the devcloud prefix (see deploy/package_offline);
# published ones under either prefix are recognized.
BUNDLE_PATTERNS = {
    role: re.compile(
        rf"^(?:{prefix}|{prefix.replace('devcloud', 'aifactory', 1)})"
        rf"-(?:v?[0-9]+\.[0-9]+\.[0-9]+-[0-9]{{8}}-)?([0-9a-f]{{12}})\.tar(?:\.gz)?$"
    )
    for role, prefix in BUNDLE_PREFIXES.items()
}
MAX_LOG_LINES = 120
ACTIVE_TASKS: set[asyncio.Task[None]] = set()


class DownloadUpdateError(RuntimeError):
    """Base class for an update request that cannot be started."""


class DownloadUpdateDisabled(DownloadUpdateError):
    """Download updates are not enabled in configuration."""


class DownloadUpdateInProgress(DownloadUpdateError):
    """Another worker already owns the cross-process update lock."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _validate_bundle_role(bundle_role: str) -> str:
    if bundle_role not in BUNDLE_ROLES:
        raise ValueError(f"Unsupported bundle role: {bundle_role}")
    return bundle_role


def _bundle_role_from_name(filename: str) -> str | None:
    for role, pattern in BUNDLE_PATTERNS.items():
        if pattern.fullmatch(filename):
            return role
    return None


class DownloadUpdateManager:
    """Coordinate one durable-status update job across Uvicorn workers."""

    @property
    def project_root(self) -> Path:
        return Path(settings.BASE_DIR).resolve()

    @property
    def download_root(self) -> Path:
        return Path(settings.DOWNLOADS_ROOT).expanduser().resolve()

    @property
    def build_root(self) -> Path:
        return Path(settings.DOWNLOAD_BUILD_ROOT).expanduser().resolve()

    @property
    def status_path(self) -> Path:
        return self.project_root / "data" / "download_update_status.json"

    def status_path_for(self, bundle_role: str) -> Path:
        bundle_role = _validate_bundle_role(bundle_role)
        if bundle_role == "server":
            return self.status_path
        return self.project_root / "data" / f"download_update_{bundle_role}_status.json"

    @property
    def lock_path(self) -> Path:
        return self.project_root / "data" / "download_update.lock"

    def current_bundle(self, bundle_role: str = "server") -> dict[str, Any] | None:
        bundle_role = _validate_bundle_role(bundle_role)
        root = self.download_root
        if not root.is_dir():
            return None
        candidates = [
            path
            for path in root.glob(f"{BUNDLE_PREFIXES[bundle_role]}-*")
            if path.is_file() and BUNDLE_PATTERNS[bundle_role].fullmatch(path.name)
            and not path.is_symlink()
        ]
        if not candidates:
            return None
        bundle = max(
            candidates,
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        checksum = bundle.with_name(bundle.name + ".sha256")
        checksum_available = checksum.is_file() and not checksum.is_symlink()
        return {
            "filename": bundle.name,
            "bundle_role": bundle_role,
            "checksum_filename": checksum.name if checksum_available else None,
            "size": bundle.stat().st_size,
            "size_display": _format_size(bundle.stat().st_size),
            "download_url": f"/download/{bundle.name}",
            "checksum_url": f"/download/{checksum.name}" if checksum_available else None,
            "modified_at": datetime.fromtimestamp(
                bundle.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        }

    def get_status(self, bundle_role: str = "server") -> dict[str, Any]:
        bundle_role = _validate_bundle_role(bundle_role)
        status_path = self.status_path_for(bundle_role)
        status: dict[str, Any] = {
            "state": "idle",
            "message": (
                "Henüz worker paketi güncellemesi başlatılmadı."
                if bundle_role == "worker"
                else "Henüz sunucu paketi güncellemesi başlatılmadı."
            ),
            "logs": [],
            "bundle_role": bundle_role,
        }
        if status_path.is_file():
            try:
                loaded = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    status.update(loaded)
            except (OSError, json.JSONDecodeError):
                logger.warning("Could not read download update status", exc_info=True)
        if status.get("state") in {"queued", "running"} and self._remove_stale_lock():
            status["state"] = "failed"
            status["message"] = (
                "Önceki indirme güncellemesi kesildi. Yeni bir güncelleme başlatın."
            )
            status["finished_at"] = _utc_now()
            logs = status.setdefault("logs", [])
            logs.append("HATA: Güncelleme worker işlemi artık çalışmıyor.")
            del logs[:-MAX_LOG_LINES]
            self._write_status(status, bundle_role)
        status["enabled"] = (
            settings.DOWNLOADS_ENABLED and settings.DOWNLOAD_UPDATES_ENABLED
        )
        status["current"] = self.current_bundle(bundle_role)
        status["target_python_version"] = (
            settings.DOWNLOAD_TARGET_PYTHON_VERSION
            or f"{sys.version_info.major}.{sys.version_info.minor}"
        )
        return status

    def start(self, bundle_role: str = "server") -> dict[str, Any]:
        bundle_role = _validate_bundle_role(bundle_role)
        if not settings.DOWNLOADS_ENABLED or not settings.DOWNLOAD_UPDATES_ENABLED:
            raise DownloadUpdateDisabled(
                "İndirme yayını devre dışı. Sunucuda sudo bash "
                "deploy/enable_downloads.sh komutunu çalıştırın."
            )
        self._acquire_lock()
        try:
            status = {
                "state": "queued",
                "message": (
                    "Worker indirme paketi güncellemesi sıraya alındı."
                    if bundle_role == "worker"
                    else "Sunucu indirme paketi güncellemesi sıraya alındı."
                ),
                "started_at": _utc_now(),
                "finished_at": None,
                "logs": ["Güncelleme yönetim API tarafından kabul edildi."],
                "bundle_role": bundle_role,
            }
            self._write_status(status, bundle_role)
            task = asyncio.create_task(self._run(status, bundle_role))
        except Exception:
            self._release_lock()
            raise
        ACTIVE_TASKS.add(task)
        task.add_done_callback(ACTIVE_TASKS.discard)
        return status

    def _acquire_lock(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._remove_stale_lock()
        payload = json.dumps(
            {"pid": os.getpid(), "started_at": _utc_now()}, indent=2
        )
        try:
            fd = os.open(
                self.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise DownloadUpdateInProgress(
                "Zaten devam eden bir indirme paketi güncellemesi var."
            ) from exc
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)

    def _release_lock(self) -> None:
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove download update lock", exc_info=True)

    def _remove_stale_lock(self) -> bool:
        if not self.lock_path.is_file():
            return False
        try:
            data = json.loads(self.lock_path.read_text(encoding="utf-8"))
            pid = int(data.get("pid", -1))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            self._release_lock()
            return True
        if pid <= 0 or not self._process_is_alive(pid):
            self._release_lock()
            return True
        return False

    def _process_is_alive(self, pid: int) -> bool:
        if os.name != "posix":
            return True
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _write_status(self, status: dict[str, Any], bundle_role: str = "server") -> None:
        status_path = self.status_path_for(bundle_role)
        status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = status_path.with_name(
            f".{status_path.name}.{uuid.uuid4().hex}.tmp"
        )
        temporary.write_text(json.dumps(status, indent=2), encoding="utf-8")
        temporary.replace(status_path)

    def _append_log(
        self,
        status: dict[str, Any],
        message: str,
        bundle_role: str = "server",
    ) -> None:
        cleaned = message.rstrip()
        if not cleaned:
            return
        logger.info("[download-update] %s", cleaned)
        logs = status.setdefault("logs", [])
        logs.append(cleaned)
        del logs[:-MAX_LOG_LINES]
        self._write_status(status, bundle_role)

    async def _run(self, status: dict[str, Any], bundle_role: str) -> None:
        try:
            status["state"] = "running"
            self._append_log(
                status,
                f"{bundle_role.capitalize()} çevrim dışı paket güncellemesi başlatıldı.",
                bundle_role,
            )
            result = await self._build_and_publish(status, bundle_role)
            status.update(result)
            status["state"] = "success"
            status["finished_at"] = _utc_now()
            self._append_log(status, result["message"], bundle_role)
        except Exception as exc:
            logger.exception("Download bundle update failed")
            status["state"] = "failed"
            status["message"] = str(exc)
            status["finished_at"] = _utc_now()
            self._append_log(status, f"ERROR: {exc}", bundle_role)
        finally:
            self._release_lock()

    async def _build_and_publish(
        self,
        status: dict[str, Any],
        bundle_role: str = "server",
    ) -> dict[str, Any]:
        bundle_role = _validate_bundle_role(bundle_role)
        commit = await self._git_commit()
        short_commit = commit[:12]
        status["source_commit"] = commit

        existing = None
        if self.download_root.is_dir():
            for path in self.download_root.glob(f"{BUNDLE_PREFIXES[bundle_role]}-*"):
                if path.is_file() and not path.is_symlink():
                    match = BUNDLE_PATTERNS[bundle_role].fullmatch(path.name)
                    if (
                        match
                        and match.group(1) == short_commit
                        and path.name.endswith(".tar.gz")
                    ):
                        existing = path
                        break

        if existing and existing.is_file() and existing.with_name(existing.name + ".sha256").is_file():
            try:
                await asyncio.to_thread(
                    self._verify_pair, existing, existing.with_name(existing.name + ".sha256")
                )
            except RuntimeError as exc:
                self._append_log(
                    status,
                    f"Mevcut paket geçersiz; yeniden oluşturuluyor: {exc}",
                    bundle_role,
                )
            else:
                return {
                    "message": f"Yayımlanan paket zaten {existing.name} olarak güncel.",
                    "published_filename": existing.name,
                }

        self.build_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="download-update-", dir=self.build_root
        ) as job_dir_text:
            job_dir = Path(job_dir_text)
            output_dir = job_dir / "output"
            temp_dir = job_dir / "tmp"
            temp_dir.mkdir()
            target_python = (
                settings.DOWNLOAD_TARGET_PYTHON_VERSION
                or f"{sys.version_info.major}.{sys.version_info.minor}"
            )
            command = [
                sys.executable,
                str(self.project_root / "deploy" / "package_offline.py"),
                "--python-version",
                target_python,
                "--bundle-role",
                bundle_role,
                "--output-dir",
                str(output_dir),
            ]
            environment = os.environ.copy()
            environment["TMPDIR"] = str(temp_dir)
            self._append_log(
                status,
                f"{short_commit} commit sürümünün {bundle_role} paketi CPython {target_python} için oluşturuluyor.",
                bundle_role,
            )
            await self._run_process(command, status, environment, bundle_role)

            archives = list(output_dir.glob(f"{BUNDLE_PREFIXES[bundle_role]}-*.tar.gz"))
            if len(archives) != 1:
                raise RuntimeError(
                    f"Bir arşiv bekleniyordu, {len(archives)} arşiv bulundu."
                )
            archive = archives[0]
            checksum = archive.with_name(archive.name + ".sha256")
            await asyncio.to_thread(self._verify_pair, archive, checksum)
            await asyncio.to_thread(
                self._publish_pair,
                archive,
                checksum,
                bundle_role,
            )
            return {
                "message": f"{archive.name} başarıyla yayımlandı.",
                "published_filename": archive.name,
            }

    async def _git_commit(self) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=self.project_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                f"Güncel Git commit belirlenemedi: {stderr.decode().strip()}"
            )
        commit = stdout.decode("ascii").strip().lower()
        if len(commit) != 40 or any(character not in "0123456789abcdef" for character in commit):
            raise RuntimeError("Git geçersiz bir kaynak commit döndürdü.")
        return commit

    async def _run_process(
        self,
        command: list[str],
        status: dict[str, Any],
        environment: dict[str, str],
        bundle_role: str = "server",
    ) -> None:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.project_root,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        status["builder_pid"] = process.pid
        self._write_status(status, bundle_role)
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            self._append_log(
                status,
                line.decode("utf-8", errors="replace"),
                bundle_role,
            )
        return_code = await process.wait()
        if return_code != 0:
            raise RuntimeError(f"Çevrim dışı paketleyici {return_code} koduyla kapandı.")

    def _verify_pair(
        self,
        archive: Path,
        checksum: Path,
        expected_filename: str | None = None,
    ) -> None:
        if (
            not archive.is_file()
            or not checksum.is_file()
            or archive.is_symlink()
            or checksum.is_symlink()
        ):
            raise RuntimeError("Oluşturulan arşiv veya checksum dosyası eksik.")
        fields = checksum.read_text(encoding="ascii").strip().split("  ", maxsplit=1)
        if len(fields) != 2 or fields[1] != (expected_filename or archive.name):
            raise RuntimeError("Oluşturulan checksum dosyasının formatı geçersiz.")
        if _sha256_file(archive) != fields[0].lower():
            raise RuntimeError("Oluşturulan arşivin checksum doğrulaması başarısız.")

    def _publish_pair(
        self,
        archive: Path,
        checksum: Path,
        bundle_role: str = "server",
    ) -> None:
        bundle_role = _validate_bundle_role(bundle_role)
        if not BUNDLE_PATTERNS[bundle_role].fullmatch(archive.name):
            raise RuntimeError("Oluşturulan arşiv adı paket rolüyle eşleşmiyor.")
        root = self.download_root
        if root == Path(root.anchor) or root == self.project_root:
            raise RuntimeError(
                "DOWNLOADS_ROOT dosya sistemi kökü veya proje kökü olamaz."
            )
        root.mkdir(parents=True, exist_ok=True)
        temporary_archive = root / f".{archive.name}.{uuid.uuid4().hex}.uploading"
        temporary_checksum = root / f".{checksum.name}.{uuid.uuid4().hex}.uploading"
        published_archive = root / archive.name
        published_checksum = root / checksum.name
        try:
            shutil.copy2(archive, temporary_archive)
            shutil.copy2(checksum, temporary_checksum)
            if os.name == "posix":
                os.chmod(temporary_archive, 0o644)
                os.chmod(temporary_checksum, 0o644)
            self._verify_pair(
                temporary_archive, temporary_checksum, expected_filename=archive.name
            )
            os.replace(temporary_archive, published_archive)
            os.replace(temporary_checksum, published_checksum)

            self.clean_old_bundles(
                preserve_filename=archive.name,
                allow_in_progress=True,
            )
        finally:
            temporary_archive.unlink(missing_ok=True)
            temporary_checksum.unlink(missing_ok=True)

    def clean_old_bundles(
        self,
        preserve_filename: str | None = None,
        *,
        allow_in_progress: bool = False,
    ) -> dict[str, Any]:
        """Remove all older/stale bundles, orphan checksums, and temporary build directories to save disk."""
        if (
            not allow_in_progress
            and self.lock_path.is_file()
            and not self._remove_stale_lock()
        ):
            raise DownloadUpdateInProgress(
                "Paket oluşturulurken disk temizliği başlatılamaz."
            )
        root = self.download_root
        cleaned_files: list[str] = []
        freed_bytes = 0

        preserved: set[str] = set()
        if preserve_filename:
            preserved.add(preserve_filename)
        for bundle_role in BUNDLE_ROLES:
            if preserve_filename and _bundle_role_from_name(preserve_filename) == bundle_role:
                continue
            current = self.current_bundle(bundle_role)
            if current:
                preserved.add(current["filename"])

        if root.is_dir():
            for prefix in BUNDLE_PREFIXES.values():
                for path in root.glob(f"{prefix}-*"):
                    if path.name in preserved or any(
                        path.name == f"{filename}.sha256" for filename in preserved
                    ):
                        continue
                    if path.is_file() or path.is_symlink():
                        try:
                            sz = path.stat().st_size
                            path.unlink()
                            cleaned_files.append(path.name)
                            freed_bytes += sz
                        except OSError:
                            pass

            # Clean any stale uploading or tmp files
            for pattern in ("*.uploading", ".*.uploading", "*.tmp", ".*.tmp"):
                for path in root.glob(pattern):
                    if path.is_file() or path.is_symlink():
                        try:
                            sz = path.stat().st_size
                            path.unlink()
                            cleaned_files.append(path.name)
                            freed_bytes += sz
                        except OSError:
                            pass

        # Clean build_root temporary directories
        if self.build_root.is_dir():
            for entry in self.build_root.glob("download-update-*"):
                if entry.is_dir():
                    try:
                        shutil.rmtree(entry, ignore_errors=True)
                        cleaned_files.append(entry.name)
                    except OSError:
                        pass

        # Clean dist/ directory in project root
        dist_dir = self.project_root / "dist"
        if dist_dir.is_dir():
            for prefix in BUNDLE_PREFIXES.values():
                for path in dist_dir.glob(f"{prefix}-*"):
                    if path.is_file():
                        try:
                            sz = path.stat().st_size
                            path.unlink()
                            cleaned_files.append(path.name)
                            freed_bytes += sz
                        except OSError:
                            pass

        logger.info(
            "Cleaned %d stale bundle files/folders, freed %s",
            len(cleaned_files),
            _format_size(freed_bytes),
        )
        return {
            "cleaned_count": len(cleaned_files),
            "freed_bytes": freed_bytes,
            "freed_display": _format_size(freed_bytes),
            "files": cleaned_files,
        }


download_update_manager = DownloadUpdateManager()
