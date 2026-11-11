"""Root-owned systemd worker for controller release uploads."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from app.installer.progress import (
    OUTPUT_FILENAME,
    PROGRESS_FILE_ENV,
    PROGRESS_FILENAME,
    ProgressReporter,
    read_output_tail,
)
from app.installer.update_source import validate_git_source


# Matches app.installer.cli.EXIT_APPLIED_PUBLISH_FAILED (kept local: this
# module runs with the system Python and imports as little as possible).
PUBLISH_FAILED_EXIT_CODE = 3
# The installer creates this; each run's output is kept there.
UPDATER_LOG_DIR = Path("/var/log/aifactory/updater")
UPDATER_LOGS_KEPT = 30


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    # The updater runs as root while the controller and worker agent run as
    # the owner of this queue directory. Preserve that ownership so they can
    # read the durable result after systemd finishes the update.
    if hasattr(os, "chown"):
        owner = path.parent.stat()
        try:
            os.chown(temporary, owner.st_uid, owner.st_gid)
        except PermissionError:
            # Non-root development/test runs already own the temporary file.
            pass
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _open_output(path: Path):
    """A fresh live output log readable by the queue directory's owner."""
    handle = path.open("w", encoding="utf-8")
    if hasattr(os, "chown"):
        owner = path.parent.stat()
        try:
            os.chown(path, owner.st_uid, owner.st_gid)
        except PermissionError:
            pass
    os.chmod(path, 0o600)
    return handle


def _archive_output(output: Path, started_at: str, log_dir: Path = UPDATER_LOG_DIR) -> None:
    """Keep the run's output beside the other AI Factory logs (newest 30)."""
    if not log_dir.is_dir() or not output.is_file():
        return
    stamp = "".join(character for character in started_at[:19] if character.isdigit())
    try:
        target = log_dir / f"update-{stamp[:8]}-{stamp[8:]}.log"
        shutil.copyfile(output, target)
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
        request = json.loads(running.read_text(encoding="utf-8"))
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
            },
        )
        progress.reset(started_at=started_at, target_version=request.get("target_version"))
        # Output goes to a file while the update runs, so the admin panel can
        # show it live; the installers report step progress to progress.json.
        env = {**os.environ, PROGRESS_FILE_ENV: str(progress.path), "PYTHONUNBUFFERED": "1"}
        with _open_output(output) as log:
            returncode = subprocess.run(
                command, text=True, stdout=log, stderr=subprocess.STDOUT, env=env
            ).returncode
        _archive_output(output, started_at)
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
                "started_at": started_at,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "filename": request.get("filename"),
                "source_type": source_type,
                "target_version": request.get("target_version"),
                "return_code": returncode,
                "output": read_output_tail(output),
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
