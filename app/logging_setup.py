"""Process logging shared by the controller and the worker agent.

Each process writes JSON lines to ``LOG_DIR/<component>/``:

- ``app.log``: application and library records
- ``audit.log``: security-relevant actions (``aifactory.audit``)
- ``access.log``: one line per HTTP request (``aifactory.access``, controller)

Files rotate at local midnight or when one file reaches a tenth of the
size cap. Rotated files are gzipped and deleted after the retention period,
or earlier (oldest first, audit last) once the directory exceeds the cap.
Admin > Günlükler changes the level, retention and forwarding at runtime.

File logging is enabled only if the installer created the directory, so
development and test runs never write to /var/log.
"""

from __future__ import annotations

import gzip
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import sys
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from app import __version__
from app.config import settings
from app.log_forwarding import (
    ACCESS_LOGGER,
    AUDIT_LOGGER,
    LEVELS,
    ForwardingConfig,
    ForwardingHandler,
    SyslogForwarder,
    SyslogFormatter,
    record_category,
    record_fields,
)

DEFAULT_RETENTION_DAYS = 14
DEFAULT_MAX_TOTAL_MB = 1024
MIN_FILE_BYTES = 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
CONSOLE_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

_ROTATED = re.compile(r"^(app|audit|access)\.log\.\d{8}-\d{6}(-\d+)?(\.gz)?$")

request_id_var: ContextVar[str] = ContextVar("aifactory_request_id", default="")


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    """Admin-managed logging policy; ``managed=False`` means env defaults."""

    managed: bool = False
    revision: int = 0
    retention_days: int = DEFAULT_RETENTION_DAYS
    max_total_mb: int = DEFAULT_MAX_TOTAL_MB
    log_level: str = ""
    forwarding: ForwardingConfig = field(default_factory=ForwardingConfig)


@dataclass(slots=True)
class RetentionPolicy:
    retention_days: int = DEFAULT_RETENTION_DAYS
    max_total_bytes: int = DEFAULT_MAX_TOTAL_MB * 1024 * 1024

    @property
    def max_file_bytes(self) -> int:
        return max(MIN_FILE_BYTES, min(MAX_FILE_BYTES, self.max_total_bytes // 10))


def default_level(component: str) -> str:
    """LOG_LEVEL, else DEBUG for a DEBUG controller, else INFO."""
    value = settings.LOG_LEVEL.strip().upper()
    if value in LEVELS:
        return value
    return "DEBUG" if settings.DEBUG and component == "controller" else "INFO"


class _CategoryFilter(logging.Filter):
    def __init__(self, *categories: str):
        super().__init__()
        self.categories = set(categories)

    def filter(self, record: logging.LogRecord) -> bool:
        return record_category(record) in self.categories


class _ContextFilter(logging.Filter):
    """Attach the current HTTP request ID to every record."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "request_id", ""):
            record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def __init__(self, component: str, hostname: str):
        super().__init__()
        self.component = component
        self.hostname = hostname

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            record_fields(record, self.component, self.hostname),
            ensure_ascii=False,
            default=str,
        )


def _compress(path: Path) -> None:
    target = path.with_name(path.name + ".gz")
    temporary = path.with_name(path.name + ".gz.tmp")
    try:
        stat = path.stat()
        with path.open("rb") as source, gzip.open(temporary, "wb") as sink:
            shutil.copyfileobj(source, sink, 1024 * 1024)
        os.chmod(temporary, stat.st_mode & 0o777)
        os.utime(temporary, (stat.st_atime, stat.st_mtime))
        os.replace(temporary, target)
        path.unlink()
    except OSError:
        temporary.unlink(missing_ok=True)


_prune_lock = threading.Lock()


def prune(directory: Path, policy: RetentionPolicy, *, now: float | None = None) -> list[str]:
    """Delete rotated files past retention, then the oldest over the size cap."""
    removed: list[str] = []
    now = time.time() if now is None else now
    cutoff = now - policy.retention_days * 86400
    with _prune_lock:
        try:
            entries = [entry for entry in directory.iterdir() if entry.is_file()]
        except OSError:
            return removed
        rotated = []
        active_bytes = 0
        for entry in entries:
            try:
                stat = entry.stat()
            except OSError:
                continue
            if _ROTATED.match(entry.name):
                if stat.st_mtime < cutoff:
                    entry.unlink(missing_ok=True)
                    removed.append(entry.name)
                else:
                    rotated.append((entry.name.startswith("audit."), stat.st_mtime, stat.st_size, entry))
            elif entry.name.endswith(".log"):
                active_bytes += stat.st_size
        total = active_bytes + sum(item[2] for item in rotated)
        # Audit history is kept longest when the cap forces deletions.
        for _, _, size, entry in sorted(rotated, key=lambda item: (item[0], item[1])):
            if total <= policy.max_total_bytes:
                break
            entry.unlink(missing_ok=True)
            removed.append(entry.name)
            total -= size
    return removed


class RetainingFileHandler(logging.handlers.BaseRotatingHandler):
    """Rotate daily or at the size limit; compress and prune in the background."""

    def __init__(self, path: Path, policy: RetentionPolicy):
        self.policy = policy
        super().__init__(str(path), "a", encoding="utf-8", delay=True)
        try:
            self._day = date.fromtimestamp(path.stat().st_mtime)
        except OSError:
            self._day = date.today()

    def _open(self):
        stream = super()._open()
        try:
            os.chmod(self.baseFilename, 0o640)
        except OSError:
            pass
        return stream

    def shouldRollover(self, record: logging.LogRecord) -> bool:
        if self.stream is None:
            self.stream = self._open()
        size = self.stream.seek(0, 2)
        if size == 0:
            self._day = date.today()
            return False
        return date.today() != self._day or size >= self.policy.max_file_bytes

    def doRollover(self) -> None:
        if self.stream:
            self.stream.close()
            self.stream = None
        base = Path(self.baseFilename)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = base.with_name(f"{base.name}.{stamp}")
        counter = 1
        while target.exists() or target.with_name(target.name + ".gz").exists():
            target = base.with_name(f"{base.name}.{stamp}-{counter}")
            counter += 1
        try:
            os.rename(base, target)
        except OSError:
            target = None
        self.stream = self._open()
        self._day = date.today()
        if target is not None:
            policy = self.policy
            threading.Thread(
                target=lambda: (_compress(target), prune(base.parent, policy)),
                name="aifactory-log-rotate",
                daemon=True,
            ).start()


class _State:
    def __init__(self, component: str):
        self.component = component
        self.hostname = socket.gethostname()
        self.policy = RetentionPolicy()
        self.level = default_level(component)
        self.revision = 0
        self.managed = False
        self.log_dir: Path | None = None
        self.file_error = ""
        self.handlers: list[logging.Handler] = []
        self.console: logging.Handler | None = None
        self.forwarding: ForwardingHandler | None = None
        self.lock = threading.Lock()


_state: _State | None = None


def _apply_levels(level: str) -> None:
    numeric = logging.getLevelName(level)
    logging.getLogger("aifactory").setLevel(numeric)
    logging.getLogger("uvicorn").setLevel(numeric)
    # Audit and access records are kept whatever the application level is.
    logging.getLogger(AUDIT_LOGGER).setLevel(logging.INFO)
    logging.getLogger(ACCESS_LOGGER).setLevel(logging.INFO)
    # Libraries (httpx, sqlalchemy, websockets) stay quiet unless debugging.
    logging.getLogger().setLevel(logging.INFO if level == "DEBUG" else logging.WARNING)


def _console_level(level: str, file_logging: bool) -> str:
    value = settings.LOG_CONSOLE_LEVEL.strip().upper()
    if value in LEVELS:
        return value
    return "WARNING" if file_logging else level


def _usable_directory(component: str) -> tuple[Path | None, str]:
    directory = Path(settings.LOG_DIR) / component
    if not directory.is_dir():
        return None, ""
    if not os.access(directory, os.W_OK | os.X_OK):
        return None, f"{directory} yazılabilir değil"
    return directory, ""


def configure_logging(component: str) -> None:
    """Install console, file and (later) forwarding handlers for this process."""
    global _state
    previous = _state
    state = _State(component)
    root = logging.getLogger()
    if previous is not None:
        for handler in previous.handlers:
            root.removeHandler(handler)
            handler.close()
        if previous.forwarding is not None:
            root.removeHandler(previous.forwarding)
            previous.forwarding.close()

    context = _ContextFilter()
    state.log_dir, state.file_error = _usable_directory(component)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT))
    console.setLevel(_console_level(state.level, state.log_dir is not None))
    console.addFilter(context)
    state.console = console
    state.handlers.append(console)

    if state.log_dir is not None:
        formatter = JsonFormatter(component, state.hostname)
        files = [("app.log", ("app",)), ("audit.log", ("audit",))]
        if component == "controller":
            files.append(("access.log", ("access",)))
        for filename, categories in files:
            handler = RetainingFileHandler(state.log_dir / filename, state.policy)
            handler.setFormatter(formatter)
            handler.addFilter(context)
            handler.addFilter(_CategoryFilter(*categories))
            state.handlers.append(handler)

    for handler in state.handlers:
        root.addHandler(handler)
    _apply_levels(state.level)

    # Uvicorn configures its own loggers before importing the app. Route them
    # through these handlers; RequestLoggingMiddleware replaces its access log.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    access.disabled = True

    _state = state
    if state.log_dir is not None:
        prune(state.log_dir, state.policy)
    if state.file_error:
        logging.getLogger("aifactory.logging").warning(
            "File logging disabled: %s", state.file_error
        )


def apply_config(config: LoggingConfig) -> None:
    """Apply an admin policy to the running process."""
    state = _state
    if state is None:
        return
    with state.lock:
        state.managed = config.managed
        state.revision = config.revision
        state.level = (
            config.log_level if config.log_level in LEVELS else default_level(state.component)
        )
        state.policy.retention_days = config.retention_days
        state.policy.max_total_bytes = config.max_total_mb * 1024 * 1024
        _apply_levels(state.level)
        if state.console is not None:
            state.console.setLevel(_console_level(state.level, state.log_dir is not None))
        _apply_forwarding(state, config.forwarding)
    if state.log_dir is not None:
        threading.Thread(
            target=prune, args=(state.log_dir, state.policy),
            name="aifactory-log-prune", daemon=True,
        ).start()


def _apply_forwarding(state: _State, config: ForwardingConfig) -> None:
    root = logging.getLogger()
    current = state.forwarding
    wanted = config.enabled and bool(config.host)
    if current is not None and (
        not wanted or current.forwarder.config.destination != config.destination
    ):
        root.removeHandler(current)
        current.close()
        current = state.forwarding = None
    if not wanted:
        return
    if current is not None:
        current.update(config)
        return
    handler = ForwardingHandler(
        SyslogForwarder(config),
        SyslogFormatter(
            config, component=state.component, hostname=state.hostname, version=__version__
        ),
    )
    handler.addFilter(_ContextFilter())
    root.addHandler(handler)
    state.forwarding = handler


def status() -> dict:
    """Logging state reported in worker heartbeats and on the admin page."""
    state = _state
    if state is None:
        return {"configured": False}
    forwarding = (
        state.forwarding.forwarder.status()
        if state.forwarding is not None
        else {"enabled": False}
    )
    return {
        "configured": True,
        "component": state.component,
        "managed": state.managed,
        "revision": state.revision,
        "level": state.level,
        "file_logging": state.log_dir is not None,
        "log_dir": str(state.log_dir or Path(settings.LOG_DIR) / state.component),
        "file_error": state.file_error,
        "retention_days": state.policy.retention_days,
        "max_total_mb": state.policy.max_total_bytes // (1024 * 1024),
        "forwarding": forwarding,
    }


def current_revision() -> tuple[bool, int]:
    state = _state
    return (state.managed, state.revision) if state is not None else (False, -1)


def component_hostname() -> tuple[str, str]:
    state = _state
    if state is None:
        return "controller", socket.gethostname()
    return state.component, state.hostname


def shutdown_forwarding() -> None:
    state = _state
    if state is not None and state.forwarding is not None:
        logging.getLogger().removeHandler(state.forwarding)
        state.forwarding.close()
        state.forwarding = None
