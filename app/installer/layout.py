"""Host names of the aifactory layout and of the legacy devcloud layout.

Releases before 3.49.1 installed everything under ``devcloud`` names. The
installer moves such hosts to the ``aifactory`` names once
(``app.installer.legacy_layout``); restore still accepts old backups. Only
the standard library is used: the root updater imports this with the system
Python.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Layout:
    name: str

    @property
    def etc_dir(self) -> str:
        return f"/etc/{self.name}"

    @property
    def var_dir(self) -> str:
        return f"/var/lib/{self.name}"

    @property
    def install_root(self) -> str:
        return f"/opt/{self.name}"

    @property
    def downloads_root(self) -> str:
        return f"/srv/{self.name}-downloads"

    @property
    def log_dir(self) -> str:
        return f"/var/log/{self.name}"

    @property
    def state_root(self) -> str:
        return f"{self.var_dir}/installer"

    @property
    def workspace_root(self) -> str:
        return f"{self.var_dir}/workspaces"

    @property
    def service_user(self) -> str:
        return self.name

    @property
    def database(self) -> str:
        """PostgreSQL role and database of the bundled database."""
        return self.name

    @property
    def postgresql_container(self) -> str:
        return f"{self.name}-postgresql"

    @property
    def env_prefix(self) -> str:
        return f"{self.name.upper()}_"

    @property
    def backup_root(self) -> str:
        return f"{self.name}-backup"

    @property
    def etc_backup_dir(self) -> str:
        return f"etc-{self.name}"

    @property
    def sqlite_filename(self) -> str:
        return f"{self.name}.db"


CURRENT = Layout("aifactory")
LEGACY = Layout("devcloud")

# Directory roots in the order they are moved.
ROOT_PAIRS = (
    (LEGACY.etc_dir, CURRENT.etc_dir),
    (LEGACY.var_dir, CURRENT.var_dir),
    (LEGACY.install_root, CURRENT.install_root),
    (LEGACY.downloads_root, CURRENT.downloads_root),
)

_LEGACY_ROOT_PATTERN = re.compile(
    r"(?<![\w.-])("
    + "|".join(re.escape(old) for old, _new in ROOT_PAIRS)
    + r")(?=/|$|[\s\"'])"
)
_NEW_ROOTS = dict(ROOT_PAIRS)


def current_path(value: str) -> str:
    """Map a path under a legacy root to the same path under the new root."""
    for old, new in ROOT_PAIRS:
        if value == old or value.startswith(old + "/"):
            return new + value[len(old) :]
    return value


def replace_legacy_roots(text: str) -> str:
    """Replace legacy roots wherever they appear inside a value, e.g. a URL."""
    return _LEGACY_ROOT_PATTERN.sub(lambda match: _NEW_ROOTS[match.group(1)], text)


def current_env_key(key: str) -> str:
    if key.startswith(LEGACY.env_prefix):
        return CURRENT.env_prefix + key[len(LEGACY.env_prefix) :]
    return key
