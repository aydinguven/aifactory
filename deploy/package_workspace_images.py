#!/usr/bin/env python3
"""Build the independently distributable AI Factory workspace image pack."""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from package_offline import (
    CONTAINER_PLATFORM,
    IMAGES,
    assert_clean_tracked_tree,
    create_archive,
    export_images,
    get_app_version,
    git_output,
    sha256_file,
    write_outer_checksum,
)


WORKSPACE_IMAGE_PACK_FORMAT = 1


def build_pack(args: argparse.Namespace) -> tuple[Path, Path]:
    root_dir = Path(__file__).resolve().parent.parent
    assert_clean_tracked_tree(root_dir)
    commit = git_output(root_dir, "rev-parse", "HEAD")
    version = get_app_version(root_dir)
    output_dir = Path(args.output_dir).resolve() if args.output_dir else root_dir / "dist"
    output_dir.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).strftime("%Y%m%d")
    output = output_dir / (
        f"devcloud-workspace-images-v{version}-{date}-{commit[:12]}.tar.gz"
    )
    output.unlink(missing_ok=True)
    output.with_name(output.name + ".sha256").unlink(missing_ok=True)

    with tempfile.TemporaryDirectory(prefix="aifactory-workspace-images-") as temp_dir:
        pack_root = Path(temp_dir) / "devcloud-workspace-images"
        images_dir = pack_root / "images"
        export_images(
            root_dir,
            images_dir,
            podman_bin=args.podman_bin,
            skip_build=args.skip_image_build,
        )
        records = []
        for archive_name, image_ref, template_id in IMAGES:
            archive = images_dir / f"{archive_name}.tar"
            records.append(
                {
                    "template_id": template_id,
                    "image_ref": image_ref,
                    "filename": f"images/{archive.name}",
                    "size": archive.stat().st_size,
                    "sha256": sha256_file(archive),
                }
            )
        manifest = {
            "workspace_image_pack_format": WORKSPACE_IMAGE_PACK_FORMAT,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_commit": commit,
            "platform": CONTAINER_PLATFORM,
            "images": records,
        }
        (pack_root / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        create_archive(pack_root, output, arcname=pack_root.name)

    checksum = write_outer_checksum(output)
    print(f"Workspace image pack: {output}")
    print(f"Checksum:             {checksum}")
    return output, checksum


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir")
    parser.add_argument("--podman-bin", default="podman")
    parser.add_argument(
        "--skip-image-build",
        action="store_true",
        help="export existing image tags instead of rebuilding them",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    build_pack(parse_args(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
