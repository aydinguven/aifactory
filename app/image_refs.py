"""Workspace image references across the devcloud → aifactory tag rename.

3.50.1 renames local workspace image tags from ``localhost/devcloud-*`` to
``localhost/aifactory-*``. Workers keep the image they already loaded and tag
it under the new name; until a worker runs 3.50.1 it still reports the old
tag. Both tags of the same image (and checksum) count as the same image.
"""

from __future__ import annotations

_PREFIXES = ("localhost/aifactory-", "localhost/devcloud-")


def canonical_image_ref(image_ref: str | None) -> str:
    value = str(image_ref or "")
    legacy = _PREFIXES[1]
    if value.startswith(legacy):
        return _PREFIXES[0] + value[len(legacy):]
    return value


def same_image_ref(left: str | None, right: str | None) -> bool:
    return canonical_image_ref(left) == canonical_image_ref(right)
