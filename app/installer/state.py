from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class InstallationState:
    schema: int
    role: str
    version: str
    installed_at: str
    updated_at: str
    configuration: dict[str, Any]

    @classmethod
    def create(cls, role: str, version: str, configuration: dict[str, Any]):
        now = datetime.now(timezone.utc).isoformat()
        return cls(1, role, version, now, now, configuration)

    @classmethod
    def load(cls, path: Path) -> "InstallationState | None":
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "schema": self.schema,
                "role": self.role,
                "version": self.version,
                "installed_at": self.installed_at,
                "updated_at": self.updated_at,
                "configuration": self.configuration,
            },
            indent=2,
        ) + "\n"
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.chmod(name, 0o600)
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
