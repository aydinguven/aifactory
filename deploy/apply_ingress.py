#!/usr/bin/env python3
"""Apply AI Factory's fixed-path Nginx ingress configuration as root."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


STAGING_ROOT = Path("/var/lib/aifactory/ingress")
DESIRED_PATH = STAGING_ROOT / "desired.json"
STAGED_CERTIFICATE = STAGING_ROOT / "certificate.pem"
STAGED_PRIVATE_KEY = STAGING_ROOT / "private-key.pem"
NGINX_CONFIG = Path("/etc/nginx/conf.d/aifactory.conf")
TLS_ROOT = Path("/etc/aifactory/tls")
ACTIVE_CERTIFICATE = TLS_ROOT / "certificate.pem"
ACTIVE_PRIVATE_KEY = TLS_ROOT / "private-key.pem"
RESULT_PATH = STAGING_ROOT / "apply-result.json"
HOSTNAME_PATTERN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def fail(message: str) -> None:
    raise RuntimeError(message)


def run(command: list[str]) -> None:
    process = subprocess.run(
        command,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if process.returncode:
        fail(process.stdout.strip() or f"{command[0]} failed with {process.returncode}")


def read_regular_file(path: Path, maximum: int) -> bytes:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        fail(f"Required staged file is missing: {path}")
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        fail(f"Staged path must be a regular, non-symlink file: {path}")
    if file_stat.st_size <= 0 or file_stat.st_size > maximum:
        fail(f"Staged file has an invalid size: {path}")
    return path.read_bytes()


def atomic_write(path: Path, content: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def proxy_location() -> str:
    return """    client_max_body_size 17G;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $aifactory_connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-Host $host;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 86400s;
        proxy_send_timeout 86400s;
    }
"""


def render_nginx_config(
    hostname: str,
    https_enabled: bool,
    http_fallback_enabled: bool,
) -> str:
    if not HOSTNAME_PATTERN.fullmatch(hostname):
        fail("desired.json contains an invalid HTTPS hostname")
    http_body = (
        proxy_location()
        if not https_enabled or http_fallback_enabled
        else f"    return 308 https://{hostname}$request_uri;\n"
    )
    blocks = [
        "map $http_upgrade $aifactory_connection_upgrade {\n"
        "    default upgrade;\n"
        "    '' close;\n"
        "}\n\n"
        "server {\n"
        "    listen 80;\n"
        "    listen [::]:80;\n"
        f"    server_name {hostname};\n\n"
        f"{http_body}"
        "}\n"
    ]
    if https_enabled:
        blocks.append(
            "\nserver {\n"
            "    listen 443 ssl;\n"
            "    listen [::]:443 ssl;\n"
            f"    server_name {hostname};\n\n"
            f"    ssl_certificate {ACTIVE_CERTIFICATE};\n"
            f"    ssl_certificate_key {ACTIVE_PRIVATE_KEY};\n"
            "    ssl_protocols TLSv1.2 TLSv1.3;\n"
            "    ssl_session_cache shared:AIFactoryTLS:10m;\n"
            "    ssl_session_timeout 1d;\n\n"
            f"{proxy_location()}"
            "}\n"
        )
    return "".join(blocks)


def restore(path: Path, content: bytes | None, mode: int) -> None:
    if content is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, content, mode)


def main() -> int:
    if len(sys.argv) != 1:
        fail("This helper accepts no command-line arguments.")
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        fail("This helper must run as root.")
    nginx = shutil.which("nginx")
    systemctl = shutil.which("systemctl")
    if not nginx or not systemctl:
        fail("Nginx and systemd must be installed before applying ingress settings.")

    desired_bytes = read_regular_file(DESIRED_PATH, 4096)
    try:
        desired = json.loads(desired_bytes)
    except json.JSONDecodeError as exc:
        fail(f"desired.json is invalid: {exc}")
    if set(desired) != {
        "https_enabled",
        "hostname",
        "http_fallback_enabled",
        "request_id",
    }:
        fail("desired.json contains unknown or missing fields.")
    https_enabled = desired["https_enabled"]
    http_fallback_enabled = desired["http_fallback_enabled"]
    hostname = desired["hostname"]
    request_id = desired["request_id"]
    if not isinstance(https_enabled, bool) or not isinstance(http_fallback_enabled, bool):
        fail("desired.json boolean fields are invalid.")
    if not isinstance(hostname, str):
        fail("desired.json hostname is invalid.")
    if not isinstance(request_id, str) or not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        request_id,
    ):
        fail("desired.json request_id is invalid.")

    certificate = private_key = None
    if https_enabled:
        certificate = read_regular_file(STAGED_CERTIFICATE, 256 * 1024)
        private_key = read_regular_file(STAGED_PRIVATE_KEY, 64 * 1024)
    config = render_nginx_config(hostname, https_enabled, http_fallback_enabled)

    managed_paths = (NGINX_CONFIG, ACTIVE_CERTIFICATE, ACTIVE_PRIVATE_KEY)
    previous = {
        path: path.read_bytes() if path.is_file() and not path.is_symlink() else None
        for path in managed_paths
    }
    try:
        NGINX_CONFIG.parent.mkdir(parents=True, exist_ok=True)
        TLS_ROOT.mkdir(parents=True, exist_ok=True)
        os.chmod(TLS_ROOT, 0o700)
        if certificate is not None and private_key is not None:
            atomic_write(ACTIVE_CERTIFICATE, certificate, 0o644)
            atomic_write(ACTIVE_PRIVATE_KEY, private_key, 0o600)
        atomic_write(NGINX_CONFIG, config.encode("utf-8"), 0o644)
        run([nginx, "-t"])
        run([systemctl, "start", "nginx"])
        run([systemctl, "reload", "nginx"])
        firewall = shutil.which("firewall-cmd")
        firewalld_active = firewall and subprocess.run(
            [systemctl, "is-active", "--quiet", "firewalld"],
            check=False,
        ).returncode == 0
        if firewall and firewalld_active:
            # Change the permanent and the runtime configuration directly.
            # Never `--reload`: it discards netavark's runtime-only rules
            # (container subnet in the trusted zone, DNS, port forwards), which
            # breaks controller -> PostgreSQL name resolution and 127.0.0.1:8000.
            run([firewall, "--permanent", "--add-service=http"])
            run([firewall, "--add-service=http"])
            if https_enabled:
                run([firewall, "--permanent", "--add-service=https"])
                run([firewall, "--add-service=https"])
    except Exception:
        restore(NGINX_CONFIG, previous[NGINX_CONFIG], 0o644)
        restore(ACTIVE_CERTIFICATE, previous[ACTIVE_CERTIFICATE], 0o644)
        restore(ACTIVE_PRIVATE_KEY, previous[ACTIVE_PRIVATE_KEY], 0o600)
        try:
            run([nginx, "-t"])
            run([systemctl, "reload", "nginx"])
        except Exception:
            pass
        raise

    print(
        f"AI Factory ingress applied: HTTP fallback={http_fallback_enabled}, "
        f"HTTPS={https_enabled}, hostname={hostname}"
    )
    atomic_write(
        RESULT_PATH,
        (json.dumps({"request_id": request_id, "success": True}) + "\n").encode(),
        0o644,
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        try:
            desired = json.loads(DESIRED_PATH.read_text(encoding="utf-8"))
            request_id = desired.get("request_id", "")
            atomic_write(
                RESULT_PATH,
                (
                    json.dumps(
                        {
                            "request_id": request_id,
                            "success": False,
                            "message": str(exc),
                        }
                    )
                    + "\n"
                ).encode(),
                0o644,
            )
        except Exception:
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
