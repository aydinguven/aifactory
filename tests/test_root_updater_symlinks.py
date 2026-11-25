"""The root updater must not follow links planted in the update queue.

The queue directory belongs to the unprivileged controller or worker account,
which can create entries there. If root followed a symlink placed under one of
its fixed file names, that account could make root overwrite (and chown to
itself) any file on the host, or copy a root-only file into a readable log.
"""

import json
import subprocess

from app.installer import progress, queued_update

SECRET = "root-only content\n"


def _victim(tmp_path):
    victim = tmp_path / "victim"
    victim.write_text(SECRET, encoding="utf-8")
    return victim


def test_status_write_does_not_follow_planted_temporary_symlinks(tmp_path):
    queue = tmp_path / "queue"
    queue.mkdir()
    victim = _victim(tmp_path)
    # The names used by earlier releases for the temporary file.
    for name in ("status.tmp", ".status.json.tmp"):
        (queue / name).symlink_to(victim)

    queued_update._write_json(queue / "status.json", {"state": "succeeded"})

    assert victim.read_text(encoding="utf-8") == SECRET
    status = queue / "status.json"
    assert not status.is_symlink()
    assert json.loads(status.read_text(encoding="utf-8")) == {"state": "succeeded"}


def test_status_write_replaces_a_symlink_instead_of_writing_through_it(tmp_path):
    queue = tmp_path / "queue"
    queue.mkdir()
    victim = _victim(tmp_path)
    (queue / "status.json").symlink_to(victim)

    queued_update._write_json(queue / "status.json", {"state": "failed"})

    assert victim.read_text(encoding="utf-8") == SECRET
    assert not (queue / "status.json").is_symlink()


def test_progress_write_does_not_follow_planted_temporary_symlink(tmp_path):
    queue = tmp_path / "queue"
    queue.mkdir()
    victim = _victim(tmp_path)
    (queue / ".progress.json.tmp").symlink_to(victim)

    progress.ProgressReporter(queue / "progress.json").reset(started_at="now")

    assert victim.read_text(encoding="utf-8") == SECRET
    assert json.loads((queue / "progress.json").read_text(encoding="utf-8"))


def _queue_git_update(queue):
    (queue / "pending.json").write_text(
        json.dumps(
            {
                "source_type": "git",
                "repository": "https://github.com/aydinguven/aifactory.git",
                "ref": "stable",
                "target_version": "3.4.5",
            }
        ),
        encoding="utf-8",
    )


def test_output_log_symlink_is_replaced_and_never_read_back(tmp_path, monkeypatch):
    queue = tmp_path / "queue"
    queue.mkdir()
    victim = _victim(tmp_path)
    _queue_git_update(queue)
    (queue / "output.log").symlink_to(victim)

    def fake_run(command, *, text, stdout, stderr, env):
        stdout.write("updated\n")
        stdout.flush()
        # Swap the log for a symlink while the installer runs: root must keep
        # using the file it created, not reopen the path.
        (queue / "output.log").unlink()
        (queue / "output.log").symlink_to(victim)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setenv("UPDATE_QUEUE_ROOT", str(queue))
    monkeypatch.setattr(queued_update.subprocess, "run", fake_run)
    monkeypatch.setattr(queued_update, "UPDATER_LOG_DIR", tmp_path / "logs")
    (tmp_path / "logs").mkdir()

    assert queued_update.main() == 0

    assert victim.read_text(encoding="utf-8") == SECRET
    status = json.loads((queue / "status.json").read_text(encoding="utf-8"))
    assert status["output"] == "updated\n"
    archived = list((tmp_path / "logs").glob("update-*.log"))
    assert [path.read_text(encoding="utf-8") for path in archived] == ["updated\n"]


def test_running_request_symlink_is_refused(tmp_path, monkeypatch):
    queue = tmp_path / "queue"
    queue.mkdir()
    secret_request = tmp_path / "root-only.json"
    secret_request.write_text(
        json.dumps({"source_type": "git", "filename": "leaked-value"}),
        encoding="utf-8",
    )
    (queue / "running.json").symlink_to(secret_request)
    ran = []
    monkeypatch.setenv("UPDATE_QUEUE_ROOT", str(queue))
    monkeypatch.setattr(queued_update.subprocess, "run", lambda *a, **k: ran.append(a))

    assert queued_update.main() == 1

    assert ran == []
    status_text = (queue / "status.json").read_text(encoding="utf-8")
    assert json.loads(status_text)["state"] == "failed"
    assert "leaked-value" not in status_text
    assert secret_request.exists()
