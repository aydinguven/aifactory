"""Live progress of a platform update for the admin panel and worker heartbeats.

The root updater, the installed CLI and the target release's installer all
write one small JSON file next to the update queue. The controller endpoint
and the worker agent read it while the update runs. Only the standard library
is used: the root updater imports this module with the system Python.

Progress is best effort: a failed write never interrupts an update.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

PROGRESS_FILE_ENV = "AIFACTORY_UPDATE_PROGRESS_FILE"
# Set by the devcloud updater (before 3.49.1) that starts the first update.
LEGACY_PROGRESS_FILE_ENV = "DEVCLOUD_UPDATE_PROGRESS_FILE"
DEFAULT_QUEUE_ROOT = "/var/lib/aifactory/update-queue"
PROGRESS_FILENAME = "progress.json"
OUTPUT_FILENAME = "output.log"

# Share of the bar for each phase; plan steps fill the range in between.
DOWNLOAD_RANGE = (0.0, 10.0)
VERIFY_RANGE = (10.0, 15.0)
STEPS_RANGE = (15.0, 98.0)

# Turkish labels for the update plan steps (app/installer/engine.py).
STEP_LABELS = {
    "preflight": "Ön kontroller",
    "backup": "Yedek alınıyor",
    "release": "Sürüm hazırlanıyor",
    "controller-images": "İmajlar yükleniyor",
    "python": "Python bağımlılıkları",
    "configuration": "Yapılandırma yazılıyor",
    "services": "Servis tanımları yazılıyor",
    "ingress": "Ingress yenileniyor",
    "migrations": "Veritabanı migration'ları",
    "restart": "Servisler yeniden başlatılıyor",
    "state": "Sürüm kaydediliyor",
    "cleanup": "Eski sürümler temizleniyor",
    "migrate-preflight": "AI Factory adlarına geçiş kontrolleri",
    "migrate-stop": "Eski servisler durduruluyor",
    "migrate-database": "Veritabanı adı değiştiriliyor",
    "migrate-move": "Dizinler taşınıyor",
    "migrate-account": "Servis hesabı yeniden adlandırılıyor",
    "migrate-files": "Yapılandırma güncelleniyor",
    "migrate-units": "Eski servis tanımları kaldırılıyor",
    "selinux": "Workspace depolaması etiketleniyor",
    "migrate-commit": "Geçiş kaydediliyor",
    "migrate-finish": "Geçiş tamamlanıyor",
}


def queue_progress_path(queue_root: str | Path | None = None) -> Path:
    root = Path(queue_root or os.environ.get("UPDATE_QUEUE_ROOT") or DEFAULT_QUEUE_ROOT)
    return root / PROGRESS_FILENAME


def _write(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    # Root writes the file, the controller or worker agent (the queue
    # directory's owner) reads it.
    if hasattr(os, "chown"):
        owner = path.parent.stat()
        try:
            os.chown(temporary, owner.st_uid, owner.st_gid)
        except PermissionError:
            pass
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class ProgressReporter:
    """Writes the progress file; every method is a no-op without a path."""

    def __init__(self, path: Path | None):
        self.path = path
        self.state: dict = {}

    @classmethod
    def from_environment(cls, fallback_root: Path | None = None) -> "ProgressReporter":
        """The root updater passes the file explicitly; installers fall back to the queue."""
        explicit = os.environ.get(PROGRESS_FILE_ENV) or os.environ.get(
            LEGACY_PROGRESS_FILE_ENV
        )
        if explicit:
            return cls(Path(explicit))
        if fallback_root is not None and fallback_root.is_dir():
            return cls(fallback_root / PROGRESS_FILENAME)
        return cls(None)

    def _emit(self, **values) -> None:
        if self.path is None:
            return
        self.state.update(values)
        self.state["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.state["percent"] = round(min(100.0, max(0.0, float(self.state.get("percent") or 0))), 1)
        try:
            if self.path.parent.is_dir():
                _write(self.path, self.state)
        except OSError:
            pass

    def reset(self, **values) -> None:
        self.state = {}
        self._emit(phase="queued", percent=0, **values)

    def download(self, done: int, total: int | None) -> None:
        low, high = DOWNLOAD_RANGE
        share = (done / total) if total else 0.0
        self._emit(
            phase="download",
            label="Bundle indiriliyor",
            bytes_done=int(done),
            bytes_total=int(total or 0),
            percent=low + (high - low) * min(1.0, share),
        )

    def verify(self, label: str = "Bundle doğrulanıyor") -> None:
        self._emit(phase="verify", label=label, percent=VERIFY_RANGE[0])

    def step(self, index: int, total: int, key: str, description: str = "") -> None:
        low, high = STEPS_RANGE
        self._emit(
            phase="steps",
            step_index=index + 1,
            step_total=total,
            step_key=key,
            label=STEP_LABELS.get(key, description or key),
            detail="",
            percent=low + (high - low) * (index / max(1, total)),
        )

    def detail(self, text: str) -> None:
        self._emit(detail=text)

    def rollback(self, error: str = "") -> None:
        self._emit(phase="rollback", label="Önceki sürüme geri dönülüyor", detail=error[-300:])

    def finish(self, succeeded: bool) -> None:
        if succeeded:
            self._emit(phase="done", label="Tamamlandı", detail="", percent=100)
        else:
            self._emit(phase="failed", label=self.state.get("label") or "Başarısız")


def read_progress(path: Path, *, newer_than: float | None = None) -> dict | None:
    """The progress file, or None when it is missing, unreadable or stale."""
    try:
        if not path.is_file() or path.is_symlink():
            return None
        if newer_than is not None and path.stat().st_mtime + 1 < newer_than:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_output_tail(path: Path, limit: int = 20000) -> str:
    """Last ``limit`` characters of the live updater output."""
    try:
        if not path.is_file() or path.is_symlink():
            return ""
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit * 4))
            return handle.read().decode("utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _timestamp(value: object) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def live_details(queue_root: Path, source: Path, value: dict) -> dict:
    """Progress (and, while running, output) that belongs to ``source``'s request.

    ``source`` is the queue file the status came from. A progress file older
    than the current request is left over from an earlier update and ignored.
    """
    details: dict = {}
    if source.name == "running.json":
        try:
            since = source.stat().st_mtime
        except OSError:
            return details
        output = queue_root / OUTPUT_FILENAME
        try:
            fresh_output = output.stat().st_mtime + 1 >= since
        except OSError:
            fresh_output = False
        if fresh_output:
            tail = read_output_tail(output)
            if tail.strip():
                details["output"] = tail
    elif source.name == "status.json":
        since = _timestamp(value.get("started_at"))
        if since is None:
            return details
    else:
        return details
    progress = read_progress(queue_root / PROGRESS_FILENAME, newer_than=since)
    if progress is not None:
        details["progress"] = progress
    return details
