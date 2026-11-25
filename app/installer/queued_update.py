"""Root-owned systemd worker for controller release uploads."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.installer.os_upgrade import run_os_upgrade
from app.installer.progress import (
    OUTPUT_FILENAME,
    PROGRESS_FILE_ENV,
    PROGRESS_FILENAME,
    ProgressReporter,
    read_tail,
    write_queue_file,
)
from app.installer.update_source import validate_git_source


# Matches app.installer.cli.EXIT_APPLIED_PUBLISH_FAILED (kept local: this
# module runs with the system Python and imports as little as possible).
PUBLISH_FAILED_EXIT_CODE = 3
# The installer creates this; each run's output is kept there.
UPDATER_LOG_DIR = Path("/var/log/aifactory/updater")
UPDATER_LOGS_KEPT = 30


# This updater runs as root, but the queue directory belongs to the
# controller or worker agent account, which can create, rename and delete
# entries in it. Every file root touches there is therefore opened without
# following symlinks, and owner and mode are set on the open descriptor.
MAX_REQUEST_BYTES = 1024 * 1024


def _write_json(path: Path, value: dict) -> None:
    # Owned like the queue directory, so the controller and worker agent can
    # read the durable result after systemd finishes the update.
    write_queue_file(path, json.dumps(value, indent=2) + "\n")


def _read_request(path: Path) -> dict:
    """The queued request, refusing symlinks and anything but a small file."""
    descriptor = os.open(
        path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    )
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REQUEST_BYTES:
            raise RuntimeError("Queued update request is not a regular file")
        request = json.loads(handle.read(MAX_REQUEST_BYTES).decode("utf-8"))
    if not isinstance(request, dict):
        raise RuntimeError("Queued update request is malformed")
    return request


def _open_output(path: Path):
    """A fresh live output log readable by the queue directory's owner.

    Any existing entry (including a planted symlink or hard link) is removed
    and the file is created anew with O_EXCL. The returned handle stays open
    for reading the tail and archiving, so root never reopens the path.
    """
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    descriptor = os.open(
        path,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        if hasattr(os, "fchown"):
            owner = os.stat(path.parent)
            try:
                os.fchown(descriptor, owner.st_uid, owner.st_gid)
            except PermissionError:
                pass
        os.fchmod(descriptor, 0o600)
    except BaseException:
        os.close(descriptor)
        raise
    return os.fdopen(descriptor, "w+", encoding="utf-8")


def _output_tail(log) -> str:
    log.flush()
    with os.fdopen(os.dup(log.fileno()), "rb") as handle:
        return read_tail(handle)


def _archive_output(log, started_at: str, log_dir: Path | None = None) -> None:
    """Keep the run's output beside the other AI Factory logs (newest 30)."""
    log_dir = log_dir or UPDATER_LOG_DIR
    if not log_dir.is_dir():
        return
    stamp = "".join(character for character in started_at[:19] if character.isdigit())
    try:
        target = log_dir / f"update-{stamp[:8]}-{stamp[8:]}.log"
        log.flush()
        with os.fdopen(os.dup(log.fileno()), "rb") as source, target.open("wb") as copy:
            source.seek(0)
            while chunk := source.read(1024 * 1024):
                copy.write(chunk)
        os.chmod(target, 0o640)
        archived = sorted(log_dir.glob("update-*.log"))
        for old in archived[:-UPDATER_LOGS_KEPT]:
            old.unlink(missing_ok=True)
    except OSError:
        pass


def main() -> int:
    root = Path(
        os.environ.get("UPDATE_QUEUE_ROOT", "/var/lib/aifactory/update-queue")
    ).resolve()
    pending = root / "pending.json"
    running = root / "running.json"
    status = root / "status.json"
    output = root / OUTPUT_FILENAME
    progress = ProgressReporter(root / PROGRESS_FILENAME)
    if running.is_file():
        # Resume the same idempotent release request after a reboot or abrupt
        # termination. Immutable release staging makes replay safe.
        pass
    elif pending.is_file():
        os.replace(pending, running)
    else:
        return 0
    try:
        request = _read_request(running)
        setup = Path(__file__).resolve().parents[2] / "deploy" / "aifactory-setup.sh"
        if not setup.is_file():
            raise RuntimeError(f"Active release installer is missing: {setup}")
        source_type = str(request.get("source_type") or "bundle")
        if source_type == "git":
            repository, ref = validate_git_source(
                str(request.get("repository") or ""),
                str(request.get("ref") or ""),
            )
            command = [
                "bash", str(setup), "--yes", "update",
                "--source-type", "git", "--repository", repository,
                "--ref", ref,
            ]
        elif source_type == "bundle":
            bundle = Path(str(request.get("bundle") or "")).resolve()
            uploads = (root / "uploads").resolve()
            if bundle.parent != uploads or not bundle.is_file() or bundle.is_symlink():
                raise RuntimeError("Queued release path is outside the upload directory")
            command = [
                "bash", str(setup), "--yes", "update", "--bundle", str(bundle)
            ]
        else:
            raise RuntimeError("Queued update source type is unsupported")
        if request.get("allow_unsigned") is True:
            command.append("--allow-unsigned")
        # Opt-in, admin-selected: upgrade OS packages before the platform.
        os_upgrade = request.get("os_upgrade") is True
        started_at = datetime.now(timezone.utc).isoformat()
        running_request = {**request, "state": "running", "started_at": started_at}
        _write_json(running, running_request)
        _write_json(
            status,
            {
                "state": "running",
                "started_at": started_at,
                "filename": request.get("filename"),
                "source_type": source_type,
                "target_version": request.get("target_version"),
                **(
                    {"os_upgrade_result": {"requested": True, "state": "pending"}}
                    if os_upgrade
                    else {}
                ),
            },
        )
        progress.reset(started_at=started_at, target_version=request.get("target_version"))
        # Output goes to a file while the update runs, so the admin panel can
        # show it live; the installers report step progress to progress.json.
        env = {**os.environ, PROGRESS_FILE_ENV: str(progress.path), "PYTHONUNBUFFERED": "1"}
        os_result: dict | None = None
        with _open_output(output) as log:
            if os_upgrade:
                os_result = run_os_upgrade(log, progress)
            if os_result is not None and os_result["state"] != "succeeded":
                # dnf rolled its own transaction back; leave the platform alone
                # too, so the host is not half-updated.
                returncode = None
            else:
                returncode = subprocess.run(
                    command, text=True, stdout=log, stderr=subprocess.STDOUT, env=env
                ).returncode
            # Read back through the handle root created, never by path.
            output_tail = _output_tail(log)
            _archive_output(log, started_at)
        if returncode is None:
            progress.finish(False)
            _write_json(
                status,
                {
                    "state": "failed",
                    "error": os_result["message"],
                    "started_at": started_at,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "filename": request.get("filename"),
                    "source_type": source_type,
                    "target_version": request.get("target_version"),
                    "os_upgrade_result": os_result,
                    "output": output_tail,
                },
            )
            return 1
        # Exit code 3: applied, but the worker bundle was not published.
        applied = returncode in (0, PUBLISH_FAILED_EXIT_CODE)
        progress.finish(applied)
        _write_json(
            status,
            {
                "state": "succeeded" if applied else "failed",
                **(
                    {
                        "warning": (
                            "Platform güncellendi ancak worker güncelleme paketi "
                            "yayınlanamadı (genellikle disk dolu). Yer açıp "
                            "'aifactory-setup.sh --yes publish-worker-bundle' çalıştırın."
                        )
                    }
                    if returncode == PUBLISH_FAILED_EXIT_CODE
                    else {}
                ),
                **({"os_upgrade_result": os_result} if os_result is not None else {}),
                # Shown on the worker inventory (reboot advice). A failed
                # platform step keeps the installer output as its message.
                **(
                    {"message": os_result["message"]}
                    if os_result is not None and applied
                    else {}
                ),
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "filename": request.get("filename"),
                "source_type": source_type,
                "target_version": request.get("target_version"),
                "return_code": returncode,
                "output": output_tail,
            },
        )
        if applied and source_type == "bundle":
            bundle.unlink(missing_ok=True)
        return 0 if applied else returncode
    except Exception as exc:
        progress.finish(False)
        _write_json(
            status,
            {
                "state": "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
            },
        )
        return 1
    finally:
        running.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
