#!/usr/bin/env python3
"""Build a small, verified patch for an already extracted air-gap bundle.

The patch preserves the base bundle's wheels and container images. It replaces
only tracked installer source files, the distribution-specific RPM payload,
and the artifact manifest.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from package_offline import (
    BUNDLE_FORMAT,
    BUNDLE_ROLES,
    BUNDLE_ROOT_NAMES,
    SYSTEM_REPOSITORY_PACKAGES_FILE,
    PackageError,
    artifact_record,
    assert_clean_tracked_tree,
    copy_tracked_source,
    download_system_rpms,
    git_output,
    run,
    sha256_file,
)


PATCH_FORMAT = 1
PATCH_SOURCE_PATHS = (
    "AIRGAP.md",
    "README.md",
    "app/installer/engine.py",
    "deploy/deploy.sh",
    "deploy/devcloud-setup.sh",
    "deploy/install_offline_system_packages.sh",
    "deploy/package_offline.py",
    "deploy/package_offline_patch.py",
)


def read_base_manifest(archive: Path, bundle_root_name: str) -> dict[str, object]:
    member = f"{bundle_root_name}/offline/MANIFEST.json"
    result = run(
        ["tar", "-xOf", str(archive), member],
        capture_output=True,
    )
    try:
        manifest = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PackageError(f"Cannot read {member} from {archive}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise PackageError("The base bundle manifest must be a JSON object")
    return manifest


def validate_base_manifest(
    manifest: dict[str, object],
    *,
    bundle_role: str,
) -> str:
    if manifest.get("bundle_format") != BUNDLE_FORMAT:
        raise PackageError("The base archive has an unsupported bundle format")
    if manifest.get("bundle_role", "server") != bundle_role:
        raise PackageError(
            f"The base archive is not a {bundle_role} bundle"
        )
    source_commit = manifest.get("source_commit")
    if (
        not isinstance(source_commit, str)
        or len(source_commit) != 40
        or any(character not in "0123456789abcdef" for character in source_commit.lower())
    ):
        raise PackageError("The base archive has an invalid source commit")
    target = manifest.get("target")
    if not isinstance(target, dict):
        raise PackageError("The base archive has no target definition")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise PackageError("The base archive has no artifact records")
    if not any(
        isinstance(record, dict) and record.get("kind") == "container-image"
        for record in artifacts
    ):
        raise PackageError("The base archive has no container image records")
    return source_commit.lower()


def replace_system_repository_records(
    manifest: dict[str, object],
    *,
    bundle_root: Path,
    source_commit: str,
    system_package_profile: dict[str, object],
) -> None:
    target = manifest["target"]
    assert isinstance(target, dict)
    old_artifacts = manifest["artifacts"]
    assert isinstance(old_artifacts, list)
    preserved_artifacts = [
        record
        for record in old_artifacts
        if not (
            isinstance(record, dict)
            and str(record.get("kind", "")).startswith("system-rpm")
        )
    ]

    system_rpm_root = bundle_root / "offline" / "system-rpms"
    checksum_path = system_rpm_root / "SHA256SUMS"
    rpms = sorted(system_rpm_root.rglob("*.rpm"))
    metadata = sorted(
        path
        for path in system_rpm_root.rglob("*")
        if path.is_file()
        and path != checksum_path
        and path.suffix.lower() != ".rpm"
    )
    required_metadata = {
        system_rpm_root
        / str(system_package_profile["profile"])
        / SYSTEM_REPOSITORY_PACKAGES_FILE,
        system_rpm_root
        / str(system_package_profile["profile"])
        / "repodata"
        / "repomd.xml",
    }
    if not rpms or not checksum_path.is_file() or not required_metadata.issubset(metadata):
        raise PackageError("The replacement local DNF repository is incomplete")

    target["system_packages"] = system_package_profile
    manifest["source_commit"] = source_commit
    manifest["created_at"] = datetime.now(timezone.utc).isoformat()
    manifest["artifacts"] = [
        *preserved_artifacts,
        *(artifact_record(bundle_root, path, "system-rpm") for path in rpms),
        *(
            artifact_record(bundle_root, path, "system-rpm-repository-metadata")
            for path in metadata
        ),
        artifact_record(bundle_root, checksum_path, "system-rpm-checksums"),
    ]


def write_patch_checksums(patch_root: Path) -> Path:
    checksum_path = patch_root / "PATCH_SHA256SUMS"
    files = sorted(
        path
        for path in patch_root.rglob("*")
        if path.is_file()
        and path != checksum_path
        and path.name != "apply-patch.sh"
    )
    checksum_path.write_text(
        "".join(
            f"{sha256_file(path)}  {path.relative_to(patch_root).as_posix()}\n"
            for path in files
        ),
        encoding="ascii",
    )
    return checksum_path


def manifest_merge_script() -> str:
    return """#!/usr/bin/env python3
import json
import sys
from pathlib import Path

if len(sys.argv) != 5:
    raise SystemExit("usage: merge-manifest.py CURRENT PATCH OUTPUT ROLE")
current_path, patch_path, output_path = map(Path, sys.argv[1:4])
expected_role = sys.argv[4]
current = json.loads(current_path.read_text(encoding="utf-8"))
patch = json.loads(patch_path.read_text(encoding="utf-8"))
for label, manifest in (("target", current), ("patch", patch)):
    if not isinstance(manifest, dict) or manifest.get("bundle_format") != 1:
        raise SystemExit(f"{label} manifest has an unsupported bundle format")
    if manifest.get("bundle_role", "server") != expected_role:
        raise SystemExit(f"{label} manifest role does not match {expected_role}")
    if not isinstance(manifest.get("target"), dict):
        raise SystemExit(f"{label} manifest has no target definition")
    if not isinstance(manifest.get("artifacts"), list):
        raise SystemExit(f"{label} manifest has no artifact list")

preserved = [
    record
    for record in current["artifacts"]
    if not (
        isinstance(record, dict)
        and str(record.get("kind", "")).startswith("system-rpm")
    )
]
replacement = [
    record
    for record in patch["artifacts"]
    if isinstance(record, dict)
    and str(record.get("kind", "")).startswith("system-rpm")
]
if not preserved or not replacement:
    raise SystemExit("cannot merge incomplete artifact manifests")
if not any(
    isinstance(record, dict) and record.get("kind") == "container-image"
    for record in preserved
):
    raise SystemExit("target manifest contains no preserved container images")

patch_target = patch["target"]
system_packages = patch_target.get("system_packages")
if not isinstance(system_packages, dict):
    raise SystemExit("patch manifest has no system package profile")
current["source_commit"] = patch["source_commit"]
current["created_at"] = patch["created_at"]
current["target"]["system_packages"] = system_packages
current["artifacts"] = [*preserved, *replacement]
output_path.write_text(json.dumps(current, indent=2) + "\\n", encoding="utf-8")
"""


def apply_script(
    *,
    bundle_root_name: str,
    bundle_role: str,
    base_commit: str,
    target_commit: str,
) -> str:
    return f"""#!/usr/bin/env bash
set -Eeuo pipefail

log() {{ printf '[devcloud-patch] %s\\n' "$*"; }}
fail() {{ printf '[devcloud-patch] ERROR: %s\\n' "$*" >&2; exit 1; }}

PATCH_DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
TARGET_INPUT="${{1:-/root/{bundle_root_name}}}"
[[ -d "${{TARGET_INPUT}}" ]] || fail "Target directory does not exist: ${{TARGET_INPUT}}"
TARGET_DIR="$(cd "${{TARGET_INPUT}}" && pwd -P)"
[[ "${{TARGET_DIR}}" != "/" ]] || fail "Refusing to patch the filesystem root."
[[ "$(basename "${{TARGET_DIR}}")" == "{bundle_root_name}" ]] || fail \
    "Expected a directory named {bundle_root_name}; got ${{TARGET_DIR}}"
[[ -f "${{TARGET_DIR}}/offline/MANIFEST.json" ]] || fail \
    "The target has no offline bundle manifest."
[[ -f "${{TARGET_DIR}}/deploy/devcloud-setup.sh" ]] || fail \
    "The target is not an extracted AI Factory bundle."
command -v sha256sum >/dev/null 2>&1 || fail "sha256sum is required."
command -v python3 >/dev/null 2>&1 || fail "python3 is required to merge bundle manifests."

cd "${{PATCH_DIR}}"
log "Verifying patch payload..."
sha256sum -c PATCH_SHA256SUMS

if grep -Fq '\"source_commit\": \"{target_commit}\"' \
    "${{TARGET_DIR}}/offline/MANIFEST.json"; then
    log "Patch is already applied; re-verifying the installed bundle."
    (cd "${{TARGET_DIR}}/offline/system-rpms" && sha256sum -c SHA256SUMS)
    python3 "${{TARGET_DIR}}/deploy/package_offline.py" \
        --verify "${{TARGET_DIR}}" \
        --expected-role {bundle_role}
    exit 0
fi

MERGED_MANIFEST="${{PATCH_DIR}}/MERGED-MANIFEST.json"
python3 "${{PATCH_DIR}}/merge-manifest.py" \
    "${{TARGET_DIR}}/offline/MANIFEST.json" \
    "${{PATCH_DIR}}/payload/{bundle_root_name}/offline/MANIFEST.json" \
    "${{MERGED_MANIFEST}}" \
    {bundle_role}

BACKUP_DIR="${{TARGET_DIR}}/offline/system-rpms.before-{target_commit[:12]}"
[[ ! -e "${{BACKUP_DIR}}" ]] || fail "Backup path already exists: ${{BACKUP_DIR}}"
log "Moving the old RPM payload to ${{BACKUP_DIR}}"
mv "${{TARGET_DIR}}/offline/system-rpms" "${{BACKUP_DIR}}"
log "Overlaying the verified {bundle_role} patch..."
cp -a "${{PATCH_DIR}}/payload/{bundle_root_name}/." "${{TARGET_DIR}}/"
cp "${{MERGED_MANIFEST}}" "${{TARGET_DIR}}/offline/MANIFEST.json"

log "Verifying the replacement local DNF repository..."
(cd "${{TARGET_DIR}}/offline/system-rpms" && sha256sum -c SHA256SUMS)
python3 "${{TARGET_DIR}}/deploy/package_offline.py" \
    --verify "${{TARGET_DIR}}" \
    --expected-role {bundle_role}

log "Patch applied successfully. The recoverable old RPM payload remains at ${{BACKUP_DIR}}"
log "Continue with: cd ${{TARGET_DIR}} && bash deploy/devcloud-setup.sh"
"""


def create_zip(patch_root: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    with zipfile.ZipFile(
        output_path,
        "w",
        compression=zipfile.ZIP_STORED,
        allowZip64=True,
    ) as archive:
        for path in sorted(patch_root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(patch_root).as_posix())


def build_patch(args: argparse.Namespace) -> Path:
    root_dir = Path(__file__).resolve().parent.parent
    assert_clean_tracked_tree(root_dir)
    source_commit = git_output(root_dir, "rev-parse", "HEAD").lower()
    bundle_role = args.bundle_role
    bundle_root_name = BUNDLE_ROOT_NAMES[bundle_role]
    archive = Path(args.base_archive).resolve()
    if not archive.is_file():
        raise PackageError(f"Base archive not found: {archive}")
    base_manifest = read_base_manifest(archive, bundle_root_name)
    base_commit = validate_base_manifest(base_manifest, bundle_role=bundle_role)
    if base_commit == source_commit:
        raise PackageError("The base archive already identifies the current source commit")

    output_dir = Path(args.output_dir).resolve()
    filename = f"{bundle_root_name}-offline-patch-compatible-to-{source_commit[:12]}.zip"
    output_path = output_dir / filename

    with tempfile.TemporaryDirectory(prefix=f"{bundle_root_name}-patch-") as temp_dir:
        patch_root = Path(temp_dir) / "patch"
        payload_root = patch_root / "payload" / bundle_root_name
        copy_tracked_source(root_dir, payload_root, PATCH_SOURCE_PATHS)
        profile = download_system_rpms(
            payload_root / "offline" / "system-rpms",
            bundle_role=bundle_role,
            dnf_bin=args.dnf_bin,
            createrepo_bin=args.createrepo_bin,
        )
        replace_system_repository_records(
            base_manifest,
            bundle_root=payload_root,
            source_commit=source_commit,
            system_package_profile=profile,
        )
        manifest_path = payload_root / "offline" / "MANIFEST.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(base_manifest, indent=2) + "\n",
            encoding="utf-8",
        )

        patch_info = {
            "patch_format": PATCH_FORMAT,
            "bundle_role": bundle_role,
            "target_directory": bundle_root_name,
            "base_source_commit": base_commit,
            "target_source_commit": source_commit,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (patch_root / "PATCH_INFO.json").write_text(
            json.dumps(patch_info, indent=2) + "\n",
            encoding="utf-8",
        )
        script_path = patch_root / "apply-patch.sh"
        script_path.write_text(
            apply_script(
                bundle_root_name=bundle_root_name,
                bundle_role=bundle_role,
                base_commit=base_commit,
                target_commit=source_commit,
            ),
            encoding="utf-8",
            newline="\n",
        )
        script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        merge_script_path = patch_root / "merge-manifest.py"
        merge_script_path.write_text(
            manifest_merge_script(),
            encoding="utf-8",
            newline="\n",
        )
        merge_script_path.chmod(
            merge_script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
        )
        write_patch_checksums(patch_root)
        create_zip(patch_root, output_path)

    print(f"Patch ZIP: {output_path}")
    print(f"SHA256: {sha256_file(output_path)}")
    return output_path


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-archive", required=True)
    parser.add_argument("--bundle-role", choices=BUNDLE_ROLES, required=True)
    parser.add_argument("--output-dir", default="dist/patches")
    parser.add_argument("--dnf-bin", default=os.getenv("DNF_BIN", "dnf"))
    parser.add_argument(
        "--createrepo-bin",
        default=os.getenv("CREATEREPO_BIN", "createrepo_c"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        build_patch(parse_args(argv))
    except PackageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
