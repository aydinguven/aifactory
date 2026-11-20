"""The browser workspace's front door: page, file transfers, WebSocket pipe."""

import importlib.util
import json
import socket
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "containers" / "browser-chromium" / "server.py"

spec = importlib.util.spec_from_file_location("aifactory_browser_server", SERVER)
server_module = importlib.util.module_from_spec(spec)
# dataclasses look the module up while it is still executing.
sys.modules[spec.name] = server_module
spec.loader.exec_module(server_module)


def start(tmp_path: Path, files: str = "both", websocket_port: int = 1):
    web = tmp_path / "web"
    web.mkdir(exist_ok=True)
    (web / "index.html").write_text("<!DOCTYPE html><title>t</title>", encoding="utf-8")
    downloads = tmp_path / "Downloads"
    downloads.mkdir(exist_ok=True)
    config = server_module.Config(
        web_root=web, files_dir=downloads, websocket_port=websocket_port, files=files
    )
    server = server_module.make_server(config, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}", downloads


def request(url, method="GET", data=None, headers=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def upload(base, name, data, headers=None):
    return request(
        f"{base}/files/{urllib.request.quote(name, safe='')}",
        method="PUT",
        data=data,
        headers={"X-AIFactory-Files": "1", **(headers or {})},
    )


@pytest.fixture
def running(tmp_path):
    server, base, downloads = start(tmp_path)
    yield base, downloads
    server.shutdown()
    server.server_close()


def test_serves_the_page_but_no_directory_listing(running):
    base, _ = running
    assert request(f"{base}/")[0] == 200
    assert request(f"{base}/core/")[0] == 404


def test_lists_downloads_without_hidden_or_partial_files(running):
    base, downloads = running
    (downloads / "rapor.txt").write_bytes(b"abc")
    (downloads / ".bashrc").write_bytes(b"x")
    (downloads / "yarim.crdownload").write_bytes(b"x")
    status, _, body = request(f"{base}/files")
    payload = json.loads(body)
    assert status == 200
    assert [item["name"] for item in payload["files"]] == ["rapor.txt"]
    assert payload["clipboard"] == "in" and payload["upload"] and payload["download"]


def test_upload_never_overwrites_and_download_round_trips(running):
    base, downloads = running
    status, _, body = upload(base, "özet.pdf", b"one")
    assert status == 201 and json.loads(body)["name"] == "özet.pdf"
    status, _, body = upload(base, "özet.pdf", b"two")
    assert json.loads(body)["name"] == "özet (1).pdf"
    assert (downloads / "özet.pdf").read_bytes() == b"one"
    # No temporary upload files are left behind.
    assert sorted(path.name for path in downloads.iterdir()) == ["özet (1).pdf", "özet.pdf"]

    status, headers, body = request(f"{base}/files/{urllib.request.quote('özet (1).pdf')}")
    assert status == 200 and body == b"two"
    assert headers["Content-Disposition"].startswith("attachment;")
    assert headers["Content-Type"] == "application/octet-stream"


def test_upload_needs_a_same_origin_request(running):
    base, downloads = running
    status, _, _ = request(f"{base}/files/a.txt", method="PUT", data=b"x")
    assert status == 403
    status, _, _ = upload(base, "a.txt", b"x", {"Sec-Fetch-Site": "cross-site"})
    assert status == 403
    assert not (downloads / "a.txt").exists()


@pytest.mark.parametrize("name", ["../kacak.txt", "..", ".bashrc", "a/b.txt", "a\nb.txt"])
def test_names_stay_inside_downloads(running, name):
    base, downloads = running
    assert upload(base, name, b"x")[0] == 400
    assert request(f"{base}/files/{urllib.request.quote(name, safe='')}")[0] == 400
    assert not (downloads.parent / "kacak.txt").exists()


def test_direction_switch_blocks_downloads(tmp_path):
    server, base, downloads = start(tmp_path, files="in")
    try:
        (downloads / "rapor.txt").write_bytes(b"abc")
        payload = json.loads(request(f"{base}/files")[2])
        assert payload["download"] is False and payload["files"] == []
        assert request(f"{base}/files/rapor.txt")[0] == 403
        assert upload(base, "yeni.txt", b"x")[0] == 201
    finally:
        server.shutdown()
        server.server_close()


def test_upload_switch_blocks_uploads(tmp_path):
    server, base, downloads = start(tmp_path, files="out")
    try:
        assert upload(base, "yeni.txt", b"x")[0] == 403
        assert not (downloads / "yeni.txt").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_websocket_upgrade_is_piped_to_websockify(tmp_path):
    backend = socket.create_server(("127.0.0.1", 0))
    seen = {}

    def websockify():
        connection, _ = backend.accept()
        with connection:
            head = b""
            while b"\r\n\r\n" not in head:
                head += connection.recv(4096)
            seen["head"] = head
            connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
            seen["frame"] = connection.recv(4096)
            connection.sendall(b"pong")

    threading.Thread(target=websockify, daemon=True).start()
    server, base, _ = start(tmp_path, websocket_port=backend.getsockname()[1])
    try:
        client = socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5)
        with client:
            client.sendall(
                b"GET /websockify HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Key: abc\r\n\r\n"
            )
            response = b""
            while b"\r\n\r\n" not in response:
                response += client.recv(4096)
            assert response.startswith(b"HTTP/1.1 101")
            client.sendall(b"ping")
            assert client.recv(4096) == b"pong"
        assert seen["head"].startswith(b"GET /websockify HTTP/1.1\r\n")
        assert b"Sec-WebSocket-Key: abc" in seen["head"]
        assert seen["frame"] == b"ping"
    finally:
        server.shutdown()
        server.server_close()
        backend.close()
