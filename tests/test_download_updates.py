import hashlib
from pathlib import Path

import pytest

from app.config import Settings, settings
from app.download_updates import (
    DownloadUpdateDisabled,
    DownloadUpdateInProgress,
    DownloadUpdateManager,
)


def _write_bundle_pair(
    root: Path,
    revision: str,
    content: bytes,
    bundle_role: str = "server",
    *,
    legacy_plain_tar: bool = False,
) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    prefix = "aifactory-worker-offline" if bundle_role == "worker" else "aifactory-offline"
    suffix = ".tar" if legacy_plain_tar else ".tar.gz"
    archive = root / f"{prefix}-v2.0.0-20260825-{revision}{suffix}"
    archive.write_bytes(content)
    checksum = archive.with_name(archive.name + ".sha256")
    checksum.write_text(
        f"{hashlib.sha256(content).hexdigest()}  {archive.name}\n",
        encoding="ascii",
    )
    return archive, checksum


def test_current_bundle_accepts_legacy_plain_tar_archive(tmp_path: Path, monkeypatch):
    download_root = tmp_path / "downloads"
    source_root = tmp_path / "source"
    source_root.mkdir()
    monkeypatch.setattr(settings, "BASE_DIR", source_root)
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(download_root))
    archive, _ = _write_bundle_pair(
        download_root,
        "abcdef123456",
        b"legacy",
        legacy_plain_tar=True,
    )

    assert DownloadUpdateManager().current_bundle()["filename"] == archive.name


def test_publish_replaces_only_older_bundle_pairs(tmp_path: Path, monkeypatch):
    download_root = tmp_path / "downloads"
    build_root = tmp_path / "build"
    source_root = tmp_path / "source"
    source_root.mkdir()
    monkeypatch.setattr(settings, "BASE_DIR", source_root)
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(download_root))
    monkeypatch.setattr(settings, "DOWNLOAD_BUILD_ROOT", str(build_root))

    old_archive, old_checksum = _write_bundle_pair(
        download_root, "111111111111", b"old bundle"
    )
    unrelated = download_root / "keep-me.txt"
    unrelated.write_text("keep", encoding="utf-8")
    staged_root = tmp_path / "staged"
    new_archive, new_checksum = _write_bundle_pair(
        staged_root, "222222222222", b"new bundle"
    )

    manager = DownloadUpdateManager()
    manager._publish_pair(new_archive, new_checksum)

    published = download_root / new_archive.name
    assert published.read_bytes() == b"new bundle"
    assert published.with_name(published.name + ".sha256").is_file()
    assert not old_archive.exists()
    assert not old_checksum.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert manager.current_bundle()["filename"] == new_archive.name


def test_publish_rejects_tampered_checksum(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(tmp_path / "downloads"))
    archive, checksum = _write_bundle_pair(tmp_path / "staged", "333333333333", b"ok")
    checksum.write_text(f"{'0' * 64}  {archive.name}\n", encoding="ascii")

    manager = DownloadUpdateManager()
    with pytest.raises(RuntimeError, match="checksum doğrulaması başarısız"):
        manager._verify_pair(archive, checksum)


def test_worker_publish_preserves_current_server_bundle(tmp_path: Path, monkeypatch):
    download_root = tmp_path / "downloads"
    build_root = tmp_path / "build"
    source_root = tmp_path / "source"
    source_root.mkdir()
    monkeypatch.setattr(settings, "BASE_DIR", source_root)
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(download_root))
    monkeypatch.setattr(settings, "DOWNLOAD_BUILD_ROOT", str(build_root))

    server_archive, server_checksum = _write_bundle_pair(
        download_root, "aaaaaaaaaaaa", b"server"
    )
    old_worker, old_worker_checksum = _write_bundle_pair(
        download_root, "bbbbbbbbbbbb", b"old worker", "worker"
    )
    new_worker, new_worker_checksum = _write_bundle_pair(
        tmp_path / "staged", "cccccccccccc", b"new worker", "worker"
    )

    manager = DownloadUpdateManager()
    manager._publish_pair(new_worker, new_worker_checksum, "worker")

    assert server_archive.exists()
    assert server_checksum.exists()
    assert not old_worker.exists()
    assert not old_worker_checksum.exists()
    assert manager.current_bundle("worker")["filename"] == new_worker.name
    assert manager.current_bundle("server")["filename"] == server_archive.name


def test_download_publisher_is_enabled_by_default():
    defaults = Settings(_env_file=None)
    assert defaults.DOWNLOADS_ENABLED is True
    assert defaults.DOWNLOAD_UPDATES_ENABLED is True


def test_start_rejects_disabled_updates(monkeypatch):
    monkeypatch.setattr(settings, "DOWNLOADS_ENABLED", True)
    monkeypatch.setattr(settings, "DOWNLOAD_UPDATES_ENABLED", False)
    with pytest.raises(DownloadUpdateDisabled):
        DownloadUpdateManager().start()

    monkeypatch.setattr(settings, "DOWNLOADS_ENABLED", False)
    monkeypatch.setattr(settings, "DOWNLOAD_UPDATES_ENABLED", True)
    with pytest.raises(DownloadUpdateDisabled):
        DownloadUpdateManager().start()


def test_clean_rejects_active_bundle_build(tmp_path: Path, monkeypatch):
    source_root = tmp_path / "source"
    source_root.mkdir()
    monkeypatch.setattr(settings, "BASE_DIR", source_root)
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(tmp_path / "downloads"))
    monkeypatch.setattr(settings, "DOWNLOAD_BUILD_ROOT", str(tmp_path / "build"))

    manager = DownloadUpdateManager()
    manager.lock_path.parent.mkdir(parents=True, exist_ok=True)
    manager.lock_path.write_text(
        f'{{"pid": {__import__("os").getpid()}}}',
        encoding="utf-8",
    )

    with pytest.raises(DownloadUpdateInProgress, match="disk temizliği"):
        manager.clean_old_bundles()


def test_clean_old_bundles_cleans_stale_files_and_preserves_current(tmp_path: Path, monkeypatch):
    download_root = tmp_path / "downloads"
    build_root = tmp_path / "build"
    source_root = tmp_path / "source"
    source_root.mkdir()
    monkeypatch.setattr(settings, "BASE_DIR", source_root)
    monkeypatch.setattr(settings, "DOWNLOADS_ROOT", str(download_root))
    monkeypatch.setattr(settings, "DOWNLOAD_BUILD_ROOT", str(build_root))

    old_archive, old_checksum = _write_bundle_pair(
        download_root, "111111111111", b"old bundle"
    )
    current_archive, current_checksum = _write_bundle_pair(
        download_root, "999999999999", b"current bundle"
    )
    worker_archive, worker_checksum = _write_bundle_pair(
        download_root, "888888888888", b"current worker", "worker"
    )
    # Stale upload file
    stale_tmp = download_root / ".stale.uploading"
    stale_tmp.write_bytes(b"temp")

    # Stale build folder
    stale_build = build_root / "download-update-12345"
    stale_build.mkdir(parents=True)
    (stale_build / "test.txt").write_text("build garbage")

    manager = DownloadUpdateManager()
    result = manager.clean_old_bundles()

    assert result["cleaned_count"] >= 3
    assert not old_archive.exists()
    assert not old_checksum.exists()
    assert not stale_tmp.exists()
    assert not stale_build.exists()
    assert current_archive.exists()
    assert current_checksum.exists()
    assert worker_archive.exists()
    assert worker_checksum.exists()
