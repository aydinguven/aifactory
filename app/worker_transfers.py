"""Worker-side temporary transfer handles, with bounded size and lifetime."""
import base64
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path

from app.agents.transfers import CHUNK_BYTES
from app.config import settings


class WorkerTransfers:
    def __init__(self, safe_path):
        self.safe_path = safe_path
        self.items = {}
        self.lock = threading.RLock()

    def expire(self, all=False):
        with self.lock:
            for key, item in list(self.items.items()):
                if all or time.monotonic() - item["touched"] > 120:
                    self.items.pop(key)["handle"].close()

    def command(self, action, payload):
        with self.lock:
            self.expire()
            key = str(payload.get("transfer_id") or "")
            if not key or len(key) > 64:
                raise ValueError("Invalid transfer ID")
            if action == "transfer.close":
                item = self.items.pop(key, None)
                if item:
                    item["handle"].close()
                return {"closed": True}
            if action == "transfer.open":
                if key in self.items or len(self.items) >= settings.WORKER_MAX_TRANSFERS:
                    raise RuntimeError("Transfer already exists or worker transfer limit reached")
                purpose = payload.get("purpose")
                target = None
                if purpose != "http":
                    target = self.safe_path(str(payload["container_name"]), str(payload.get("path") or ""))
                limit = settings.PROXY_MAX_REQUEST_BYTES if purpose == "http" else settings.FILE_TRANSFER_MAX_BYTES
                if purpose == "download":
                    handle = target.open("rb")
                    size = os.fstat(handle.fileno()).st_size
                    if size > limit:
                        handle.close()
                        raise ValueError("File exceeds transfer limit")
                elif purpose in {"upload", "http"}:
                    if target is not None and not target.parent.is_dir():
                        raise FileNotFoundError("Upload directory does not exist")
                    handle = tempfile.TemporaryFile()
                    size = 0
                else:
                    raise ValueError("Unknown transfer purpose")
                self.items[key] = {"handle": handle, "target": target, "purpose": purpose,
                                   "size": size, "limit": limit, "finished": False,
                                   "touched": time.monotonic()}
                return {"size": size, "name": target.name if target else ""}
            item = self.items[key]
            item["touched"] = time.monotonic()
            handle = item["handle"]
            if action == "transfer.write":
                if item["purpose"] == "download" or item["finished"]:
                    raise ValueError("Transfer is not writable")
                if len(payload.get("data", "")) > 4 * ((CHUNK_BYTES + 2) // 3):
                    raise ValueError("Chunk is too large")
                data = base64.b64decode(payload["data"], validate=True)
                if len(data) > CHUNK_BYTES or item["size"] + len(data) > item["limit"]:
                    raise ValueError("Transfer exceeds size limit")
                if payload.get("offset") != item["size"]:
                    raise ValueError("Unexpected transfer offset")
                handle.write(data)
                item["size"] += len(data)
                return {"size": item["size"]}
            if action == "transfer.read":
                if item["purpose"] != "download":
                    raise ValueError("Transfer is not readable")
                offset = int(payload["offset"])
                if not 0 <= offset <= item["size"]:
                    raise ValueError("Invalid transfer offset")
                handle.seek(offset)
                data = handle.read(min(CHUNK_BYTES, item["size"] - offset))
                return {"data": base64.b64encode(data).decode("ascii")}
            if action == "transfer.finish":
                if item["purpose"] == "download":
                    raise ValueError("Cannot finish a download")
                if not item["finished"] and item["purpose"] == "upload":
                    target = item["target"]
                    # Atomic replacement never follows a pre-existing leaf symlink.
                    descriptor, temporary = tempfile.mkstemp(prefix=".aifactory-upload-", dir=target.parent)
                    try:
                        with os.fdopen(descriptor, "wb") as output:
                            handle.seek(0)
                            shutil.copyfileobj(handle, output, CHUNK_BYTES)
                            output.flush()
                            os.fsync(output.fileno())
                        os.replace(temporary, target)
                    finally:
                        Path(temporary).unlink(missing_ok=True)
                item["finished"] = True
                return {"size": item["size"], "name": item["target"].name if item["target"] else ""}
            raise ValueError("Unknown transfer command")

    def take_http_body(self, key):
        with self.lock:
            item = self.items[key]
            if item["purpose"] != "http" or not item["finished"]:
                raise ValueError("HTTP body transfer is incomplete")
            self.items.pop(key)
            item["handle"].seek(0)
            return item["handle"]
