#!/usr/bin/env python3
"""Front door of the browser workspace, on the one port the proxy tunnels.

- ``/`` and the noVNC library: the client page (static files).
- ``/files``: the Downloads folder, so files can be uploaded into the remote
  browser and downloaded from it. ``GET /files`` lists it together with what
  the workspace allows, ``GET /files/<name>`` downloads one file and
  ``PUT /files/<name>`` uploads one (the raw body is the file).
- Any WebSocket upgrade: piped byte for byte to websockify, which listens on
  loopback only and bridges to the VNC socket.

Standard library only: the image's python3 comes with websockify.
"""

from __future__ import annotations

import http.server
import json
import os
import select
import socket
import stat
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

CHUNK = 1024 * 1024
# Chromium's partial downloads; they are not files yet.
PARTIAL_SUFFIXES = (".crdownload",)
UPLOAD_PREFIX = ".aifactory-upload-"
# A cross-site page cannot send a custom header without a CORS preflight.
WRITE_HEADER = "X-AIFactory-Files"


def _direction(value: str | None, default: str) -> str:
    """Normalise a both/in/out/none switch; anything else means both."""
    value = (value or default).strip().lower()
    return value if value in {"both", "in", "out", "none"} else "both"


@dataclass(frozen=True)
class Config:
    web_root: Path
    files_dir: Path
    websocket_port: int
    clipboard: str = "in"
    files: str = "both"

    @property
    def can_upload(self) -> bool:
        return self.files in {"both", "in"}

    @property
    def can_download(self) -> bool:
        return self.files in {"both", "out"}

    @classmethod
    def from_env(cls) -> "Config":
        home = Path(os.environ.get("HOME", "/home/browser"))
        return cls(
            web_root=Path(os.environ.get("AIFACTORY_BROWSER_WEB_ROOT", "/opt/aifactory-browser/web")),
            files_dir=Path(os.environ.get("AIFACTORY_BROWSER_FILES_DIR", str(home / "Downloads"))),
            websocket_port=int(os.environ.get("AIFACTORY_BROWSER_WEBSOCKIFY_PORT", "6081")),
            # Defaults match entrypoint.sh.
            clipboard=_direction(os.environ.get("AIFACTORY_BROWSER_CLIPBOARD"), "in"),
            files=_direction(os.environ.get("AIFACTORY_BROWSER_FILES"), "both"),
        )


def log(message: str) -> None:
    print(f"aifactory-browser: {message}", file=sys.stderr, flush=True)


def clean_name(raw: str) -> str | None:
    """A plain file name inside the Downloads folder, or None."""
    name = urllib.parse.unquote(raw)
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        return None
    if len(name.encode("utf-8", "surrogateescape")) > 255:
        return None
    return name


def content_disposition(name: str) -> str:
    fallback = "".join(
        char if 32 <= ord(char) < 127 and char not in '"\\;' else "_" for char in name
    )
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"


def numbered(name: str, index: int) -> str:
    """report.pdf -> report (1).pdf, the way browsers name a second copy."""
    stem, dot, suffix = name.rpartition(".")
    if not dot or not stem:
        return f"{name} ({index})"
    return f"{stem} ({index}).{suffix}"


def make_handler(config: Config):
    class Handler(http.server.SimpleHTTPRequestHandler):
        server_version = "aifactory-browser"
        sys_version = ""
        protocol_version = "HTTP/1.1"
        # Unbuffered: after a WebSocket upgrade no client bytes may be left
        # behind in a read buffer the pipe does not see.
        rbufsize = 0

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(config.web_root), **kwargs)

        def log_message(self, format, *args):  # noqa: A002 - stdlib signature
            pass  # file transfers are logged explicitly; the rest is noise

        # ---- routing -------------------------------------------------
        def _route(self) -> str | None:
            """The file name part of /files/<name>, "" for /files, else None."""
            path = urllib.parse.urlsplit(self.path).path
            if path == "/files":
                return ""
            if path.startswith("/files/"):
                return path[len("/files/"):]
            return None

        def _is_upgrade(self) -> bool:
            return "websocket" in self.headers.get("Upgrade", "").lower()

        def do_GET(self):
            if self._is_upgrade():
                self._proxy_websocket()
                return
            route = self._route()
            if route is None:
                super().do_GET()
            elif route == "":
                self._list_files()
            else:
                self._download(route)

        def do_HEAD(self):
            if self._route() is None:
                super().do_HEAD()
            else:
                self.send_error(405)

        def do_PUT(self):
            # A refused upload leaves its body unread on the connection.
            self.close_connection = True
            route = self._route()
            if not route:
                self.send_error(405)
                return
            self._upload(route)

        def list_directory(self, path):
            self.send_error(404)
            return None

        # ---- helpers -------------------------------------------------
        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _fail(self, status: int, message: str) -> None:
            self._send_json(status, {"error": message})

        def _write_allowed(self) -> bool:
            if self.headers.get(WRITE_HEADER) != "1":
                return False
            site = self.headers.get("Sec-Fetch-Site")
            return site in (None, "same-origin")

        # ---- files ---------------------------------------------------
        def _list_files(self) -> None:
            files = []
            if config.can_download:
                try:
                    entries = list(os.scandir(config.files_dir))
                except OSError:
                    entries = []
                for entry in entries:
                    if entry.name.startswith(".") or entry.name.endswith(PARTIAL_SUFFIXES):
                        continue
                    try:
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        info = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    files.append({"name": entry.name, "size": info.st_size, "modified": int(info.st_mtime)})
                files.sort(key=lambda item: item["modified"], reverse=True)
            self._send_json(200, {
                "clipboard": config.clipboard,
                "upload": config.can_upload,
                "download": config.can_download,
                "files": files,
            })

        def _download(self, raw: str) -> None:
            if not config.can_download:
                self._fail(403, "Dosya indirme bu workspace'te kapalı.")
                return
            name = clean_name(raw)
            if name is None:
                self._fail(400, "Geçersiz dosya adı.")
                return
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(config.files_dir / name, flags)
            except OSError:
                self._fail(404, "Dosya bulunamadı.")
                return
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode):
                    self._fail(404, "Dosya bulunamadı.")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(info.st_size))
                self.send_header("Content-Disposition", content_disposition(name))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                remaining = info.st_size
                while remaining > 0:
                    chunk = source.read(min(CHUNK, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            log(f"file downloaded: {name} ({info.st_size} bytes)")

        def _upload(self, raw: str) -> None:
            if not config.can_upload:
                self._fail(403, "Dosya yükleme bu workspace'te kapalı.")
                return
            if not self._write_allowed():
                self._fail(403, "İstek reddedildi.")
                return
            name = clean_name(raw)
            if name is None:
                self._fail(400, "Geçersiz dosya adı.")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._fail(411, "Dosya boyutu bilinmiyor.")
                return
            if length < 0:
                self._fail(400, "Geçersiz dosya boyutu.")
                return

            config.files_dir.mkdir(parents=True, exist_ok=True)
            received = 0
            try:
                fd, temporary_name = tempfile.mkstemp(prefix=UPLOAD_PREFIX, dir=config.files_dir)
            except OSError:
                self._fail(500, "Dosya kaydedilemedi.")
                return
            temporary = Path(temporary_name)
            try:
                with os.fdopen(fd, "wb") as target:
                    while received < length:
                        chunk = self.rfile.read(min(CHUNK, length - received))
                        if not chunk:
                            break
                        target.write(chunk)
                        received += len(chunk)
                if received != length:
                    self._fail(400, "Dosya eksik geldi.")
                    return
                final = self._claim_name(temporary, name)
            except OSError as exc:
                self._fail(507 if exc.errno == 28 else 500, "Dosya kaydedilemedi.")
                return
            finally:
                temporary.unlink(missing_ok=True)
            log(f"file uploaded: {final} ({received} bytes)")
            self._send_json(201, {"name": final, "size": received})

        @staticmethod
        def _claim_name(temporary: Path, name: str) -> str:
            """Hard-link the upload under a free name; never overwrite a file."""
            for index in range(0, 1000):
                candidate = name if index == 0 else numbered(name, index)
                try:
                    os.link(temporary, config.files_dir / candidate)
                    return candidate
                except FileExistsError:
                    continue
            raise OSError("no free file name")

        # ---- VNC WebSocket -------------------------------------------
        def _proxy_websocket(self) -> None:
            try:
                upstream = socket.create_connection(("127.0.0.1", config.websocket_port), timeout=10)
            except OSError:
                self.send_error(502)
                return
            upstream.settimeout(None)
            self.close_connection = True
            head = [self.requestline]
            head += [f"{key}: {value}" for key, value in self.headers.items()]
            client = self.connection
            try:
                upstream.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
                sockets = [client, upstream]
                while True:
                    readable, _, _ = select.select(sockets, [], [])
                    for source in readable:
                        data = source.recv(65536)
                        if not data:
                            return
                        (upstream if source is client else client).sendall(data)
            except OSError:
                pass
            finally:
                upstream.close()

    return Handler


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(config: Config, host: str, port: int) -> Server:
    return Server((host, port), make_handler(config))


def main() -> None:
    config = Config.from_env()
    port = int(os.environ.get("AIFACTORY_BROWSER_PORT", "6080"))
    server = make_server(config, "0.0.0.0", port)
    log(
        f"listening on {port} (clipboard: {config.clipboard}, files: {config.files})"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
