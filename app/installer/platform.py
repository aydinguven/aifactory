from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class InstallerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HostPlatform:
    distribution: str
    version_id: str
    major_version: str
    architecture: str

    @property
    def profile(self) -> str:
        return f"{self.distribution}-{self.major_version}-{self.architecture}"


def detect_platform(os_release: Path = Path("/etc/os-release")) -> HostPlatform:
    if not os_release.is_file():
        raise InstallerError(f"Operating-system metadata is missing: {os_release}")
    values: dict[str, str] = {}
    for line in os_release.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Z_]+)=(.*)$", line)
        if not match:
            continue
        values[match.group(1)] = match.group(2).strip().strip('"\'')
    distribution = values.get("ID", "").lower()
    version_id = values.get("VERSION_ID", "")
    return HostPlatform(
        distribution=distribution,
        version_id=version_id,
        major_version=version_id.split(".", 1)[0],
        architecture=platform.machine().lower(),
    )


SUPPORTED_MAJOR_VERSIONS = ("9", "10")

# EL9's platform python3 is 3.9, but AI Factory needs 3.11+ and ships cp312
# wheels, so EL9 hosts use the AppStream python3.12 interpreter instead.
_HOST_PYTHON_BY_MAJOR = {"9": "/usr/bin/python3.12"}
_HOST_PYTHON_PACKAGES_BY_MAJOR = {"9": ("python3.12", "python3.12-pip")}


def host_python(host: HostPlatform | None) -> str:
    """Return the interpreter AI Factory runs under on this host."""
    major = host.major_version if host else ""
    return _HOST_PYTHON_BY_MAJOR.get(major, "/usr/bin/python3")


def host_python_packages(host: HostPlatform | None) -> tuple[str, ...]:
    """Return extra DNF packages that provide the host_python interpreter."""
    major = host.major_version if host else ""
    return _HOST_PYTHON_PACKAGES_BY_MAJOR.get(major, ())


def validate_target(host: HostPlatform) -> None:
    if host.distribution not in {"rocky", "rhel"}:
        raise InstallerError(
            f"Only Rocky Linux and RHEL 9/10 are supported; detected {host.distribution or 'unknown'}."
        )
    if host.major_version not in SUPPORTED_MAJOR_VERSIONS:
        raise InstallerError(
            f"Only major versions 9 and 10 are supported; detected {host.version_id or 'unknown'}."
        )
    if host.architecture not in {"x86_64", "amd64"}:
        raise InstallerError(
            f"Only x86_64 is supported; detected {host.architecture}."
        )


class CommandRunner:
    def __init__(self, *, dry_run: bool = False):
        self.dry_run = dry_run
        self.commands: list[list[str]] = []

    def run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        capture_output: bool = False,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if self.dry_run:
            return subprocess.CompletedProcess(command, 0, "", "")
        try:
            return subprocess.run(
                command,
                cwd=cwd,
                env={**os.environ, **(env or {})},
                check=check,
                text=True,
                capture_output=capture_output,
            )
        except FileNotFoundError as exc:
            raise InstallerError(f"Required command is missing: {command[0]}") from exc
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise InstallerError(
                f"Command failed ({exc.returncode}): {' '.join(command)}"
                + (f"\n{detail}" if detail else "")
            ) from exc

    def exists(self, command: str) -> bool:
        return shutil.which(command) is not None
