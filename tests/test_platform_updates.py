import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from app.installer.platform import CommandRunner, InstallerError
from app.installer.update_source import (
    read_channel,
    resolve_update_bundle,
    validate_git_source,
    write_channel,
)
from app.platform_release import (
    load_platform_release,
    publish_platform_bundle,
    publish_platform_root,
)
from app.release_catalog import latest_release
from deploy.build_platform_update import _image_digest


def _platform_root(root: Path) -> Path:
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text(
        '__version__ = "3.4.0"\n', encoding="utf-8"
    )
    images = {}
    for role, content in (("controller", b"controller-image"), ("worker", b"worker-image")):
        archive = root / f"offline/{role}-images/aifactory-{role}.tar"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(content)
        images[role] = {
            "image": f"localhost/aifactory-{role}:3.4.0",
            "source": f"quay.io/example/aifactory:{role}-3.4.0",
            "digest": "sha256:" + ("a" if role == "controller" else "b") * 64,
            "archive": archive.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }
    (root / "platform-release.json").write_text(
        json.dumps(
            {
                "format": 1,
                "version": "3.4.0",
                "source_commit": "abcdef123456",
                "workspace_images_included": False,
                "images": images,
            }
        ),
        encoding="utf-8",
    )
    return root


def test_platform_manifest_requires_verified_images_and_excludes_workspaces(tmp_path):
    root = _platform_root(tmp_path / "release")

    release = load_platform_release(root)

    assert release.controller.archive.endswith("aifactory-controller.tar")
    assert release.worker.archive.endswith("aifactory-worker.tar")
    manifest = json.loads((root / "platform-release.json").read_text(encoding="utf-8"))
    manifest["workspace_images_included"] = True
    (root / "platform-release.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InstallerError, match="must not contain workspace images"):
        load_platform_release(root)


def test_platform_bundle_publication_is_visible_to_workers(tmp_path):
    bundle = tmp_path / "aifactory-platform-update-v3.4.0-abcdef1.tar.gz"
    bundle.write_bytes(b"signed-platform-bundle")
    downloads = tmp_path / "downloads"

    published = publish_platform_bundle(bundle, downloads)
    latest = latest_release(downloads)

    assert published.read_bytes() == bundle.read_bytes()
    assert latest is not None
    assert latest.path == published
    assert latest.version == "3.4.0"
    assert published.with_name(published.name + ".sha256").is_file()


def test_clean_install_tree_is_published_as_verified_worker_bundle(tmp_path):
    root = _platform_root(tmp_path / "release")
    artifacts = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        artifacts.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size,
            }
        )
    (root / "release.json").write_text(
        json.dumps(
            {
                "format": 1,
                "version": "3.4.0",
                "source_commit": "abcdef123456",
                "artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )

    published = publish_platform_root(root, tmp_path / "downloads")

    assert published.name == "aifactory-platform-update-v3.4.0-abcdef123456.tar.gz"
    assert latest_release(tmp_path / "downloads").path == published
    with tarfile.open(published, "r:gz") as archive:
        members = {member.name for member in archive.getmembers() if member.isfile()}
    assert "aifactory-3.4.0/release.json" in members
    assert "aifactory-3.4.0/platform-release.json" in members
    assert "aifactory-3.4.0/offline/worker-images/aifactory-worker.tar" in members


def test_git_channel_resolves_a_checksum_pinned_relative_bundle(tmp_path):
    repository = tmp_path / "channel"
    repository.mkdir()
    bundle = repository / "aifactory-platform-update-v3.4.0-abcdef1.tar.gz"
    bundle.write_bytes(b"immutable-release")
    channel_path = write_channel(bundle, repository / "aifactory-update-channel.json", url=bundle.name)
    assert read_channel(repository).sha256 == hashlib.sha256(bundle.read_bytes()).hexdigest()
    subprocess.run(["git", "init", "-q", "-b", "stable"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    subprocess.run(["git", "add", bundle.name, channel_path.name], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-qm", "release channel"], cwd=repository, check=True)

    with resolve_update_bundle(
        source_type="git",
        location=repository.as_uri(),
        ref="stable",
        runner=CommandRunner(),
    ) as resolved:
        assert resolved.read_bytes() == b"immutable-release"


@pytest.mark.parametrize(
    ("location", "ref"),
    [
        ("ext::sh -c evil", "stable"),
        ("http://insecure.example/aifactory.git", "stable"),
        ("https://example.com/aifactory.git", "--upload-pack=evil"),
        ("https://example.com/aifactory.git", "refs/heads/../evil"),
    ],
)
def test_git_channel_rejects_unsafe_transport_or_ref(location, ref):
    with pytest.raises(InstallerError):
        validate_git_source(location, ref)


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("a" * 64, "sha256:" + "a" * 64),
        ("sha256:" + "b" * 64, "sha256:" + "b" * 64),
    ],
)
def test_image_digest_accepts_current_and_legacy_podman_formats(
    monkeypatch, tmp_path, reported, expected
):
    monkeypatch.setattr("deploy.build_platform_update.command", lambda *_args: reported)

    assert _image_digest(tmp_path, "podman", "localhost/example:latest") == expected


def test_publishing_prunes_old_bundles_and_refuses_without_disk_space(tmp_path, monkeypatch):
    import shutil as shutil_module

    from app import platform_release
    from app.installer.platform import InstallerError

    releases = tmp_path / "downloads" / "releases"
    releases.mkdir(parents=True)
    for index, version in enumerate(("3.12.0", "3.13.0", "3.14.0")):
        old = releases / f"aifactory-platform-update-v{version}-{'a' * 12}.tar.gz"
        old.write_bytes(b"old")
        (releases / f"{old.name}.sha256").write_text("x", encoding="ascii")
        os.utime(old, (1000 + index, 1000 + index))
    (releases / ".aifactory-platform-update-v3.15.0-bbbbbbbbbbbb.tar.gz.dead.partial").write_bytes(b"half")
    bundle = tmp_path / "aifactory-platform-update-v3.15.0-bbbbbbbbbbbb.tar.gz"
    bundle.write_bytes(b"new bundle")

    real_usage = shutil_module.disk_usage
    monkeypatch.setattr(
        platform_release.shutil, "disk_usage",
        lambda path: real_usage(path)._replace(free=1024),
    )
    with pytest.raises(InstallerError, match="Not enough free disk space"):
        platform_release.publish_platform_bundle(bundle, tmp_path / "downloads")
    # Old bundles beyond the previous one and the interrupted copy are gone
    # before the space check, so a retry has more room.
    assert sorted(path.name for path in releases.iterdir()) == [
        "aifactory-platform-update-v3.14.0-aaaaaaaaaaaa.tar.gz",
        "aifactory-platform-update-v3.14.0-aaaaaaaaaaaa.tar.gz.sha256",
    ]

    monkeypatch.setattr(platform_release.shutil, "disk_usage", real_usage)
    published = platform_release.publish_platform_bundle(bundle, tmp_path / "downloads")
    assert published.read_bytes() == b"new bundle"
    assert {path.name for path in releases.iterdir() if not path.name.endswith(".sha256")} == {
        "aifactory-platform-update-v3.14.0-aaaaaaaaaaaa.tar.gz",
        published.name,
    }
