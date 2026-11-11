"""Send log records to a syslog collector such as IBM QRadar.

Each controller and worker process forwards its own records over UDP, TCP or
TLS, so host rsyslog configuration (often managed centrally) stays untouched.
Only the standard library is used.

Records wait in a bounded in-memory queue while the collector is
unreachable; once the queue is full the oldest record is dropped and
counted. The local files in /var/log/aifactory remain the full record.
"""

from __future__ import annotations

import collections
import json
import logging
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone

PROTOCOLS = ("udp", "tcp", "tls")
FORMATS = ("leef", "rfc5424")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")

AUDIT_LOGGER = "aifactory.audit"
ACCESS_LOGGER = "aifactory.access"

# local0. QRadar groups events by the syslog header host, not the facility.
SYSLOG_FACILITY = 16
# Collectors commonly cap one event at 4-8 KiB; long fields are cut first.
MAX_MESSAGE_BYTES = 8192
MAX_FIELD_CHARS = 3000
QUEUE_SIZE = 10_000
CONNECT_TIMEOUT_SECONDS = 5.0
SEND_TIMEOUT_SECONDS = 10.0
MAX_BACKOFF_SECONDS = 60.0

LEEF_VENDOR = "AIFactory"
LEEF_PRODUCT = "AI Factory"
LEEF_DELIMITER = "^"
LEEF_TIME_FORMAT = "yyyy-MM-dd'T'HH:mm:ss.SSSXXX"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

logger = logging.getLogger("aifactory.logging")


@dataclass(frozen=True, slots=True)
class ForwardingConfig:
    enabled: bool = False
    host: str = ""
    port: int = 514
    protocol: str = "tcp"
    format: str = "leef"
    tls_verify: bool = True
    ca_pem: str = ""
    audit: bool = True
    app: bool = True
    app_min_level: str = "WARNING"
    access: bool = False

    def accepts(self, category: str, levelno: int) -> bool:
        if category == "audit":
            return self.audit
        if category == "access":
            return self.access
        return self.app and levelno >= logging.getLevelName(self.app_min_level)

    @property
    def destination(self) -> tuple:
        """Fields that require a new connection when they change."""
        return (self.host, self.port, self.protocol, self.tls_verify, self.ca_pem)


def record_category(record: logging.LogRecord) -> str:
    name = record.name
    if name == AUDIT_LOGGER or name.startswith(AUDIT_LOGGER + "."):
        return "audit"
    if name == ACCESS_LOGGER or name.startswith(ACCESS_LOGGER + "."):
        return "access"
    return "app"


def _syslog_severity(levelno: int) -> int:
    if levelno >= logging.CRITICAL:
        return 2
    if levelno >= logging.ERROR:
        return 3
    if levelno >= logging.WARNING:
        return 4
    if levelno >= logging.INFO:
        return 6
    return 7


def _leef_severity(levelno: int) -> int:
    """LEEF sev is 1 (lowest) to 10."""
    if levelno >= logging.CRITICAL:
        return 9
    if levelno >= logging.ERROR:
        return 7
    if levelno >= logging.WARNING:
        return 5
    if levelno >= logging.INFO:
        return 3
    return 1


def _clip(value: str, limit: int = MAX_FIELD_CHARS) -> str:
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _header_host(hostname: str) -> str:
    """Syslog HOSTNAME: printable ASCII without spaces."""
    cleaned = re.sub(r"[^\x21-\x7e]", "", hostname)[:255]
    return cleaned or "-"


def record_fields(record: logging.LogRecord, component: str, hostname: str) -> dict:
    """The JSON shape shared by local files and the RFC 5424 body."""
    data: dict = {
        "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z"),
        "level": record.levelname,
        "component": component,
        "host": hostname,
        "logger": record.name,
        "msg": record.getMessage(),
    }
    request_id = getattr(record, "request_id", "")
    if request_id:
        data["request_id"] = request_id
    event = getattr(record, "event", None)
    if isinstance(event, dict) and event:
        data["event"] = event
    if record.exc_info:
        data["exc"] = logging.Formatter().formatException(record.exc_info)
    elif record.exc_text:
        data["exc"] = record.exc_text
    return data


def _leef_value(value: object) -> str:
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, default=str)
    text = str(value)
    for character in ("\r", "\n", "\t", LEEF_DELIMITER):
        text = text.replace(character, " ")
    return _clip(text)


def _leef_header_field(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|")


def _leef_key(name: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", name)
    head, *rest = [part for part in parts if part] or ["field"]
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


class SyslogFormatter:
    """Render one record as a syslog line in LEEF 2.0 or RFC 5424 + JSON."""

    def __init__(self, config: ForwardingConfig, *, component: str, hostname: str, version: str):
        self.config = config
        self.component = component
        self.hostname = hostname
        self.version = version

    def event_id(self, record: logging.LogRecord, category: str) -> str:
        event = getattr(record, "event", None)
        if category == "audit" and isinstance(event, dict) and event.get("action"):
            return str(event["action"])
        if category == "access":
            return "http.request"
        return f"{record.name}.{record.levelname.lower()}"

    def format(self, record: logging.LogRecord) -> bytes:
        category = record_category(record)
        if self.config.format == "rfc5424":
            line = self._rfc5424(record, category)
        else:
            line = self._leef(record, category)
        payload = line.encode("utf-8", errors="replace")
        if len(payload) > MAX_MESSAGE_BYTES:
            payload = payload[:MAX_MESSAGE_BYTES].decode("utf-8", errors="ignore").encode("utf-8")
        return payload

    def _pri(self, record: logging.LogRecord) -> str:
        return f"<{SYSLOG_FACILITY * 8 + _syslog_severity(record.levelno)}>"

    def _rfc5424(self, record: logging.LogRecord, category: str) -> str:
        data = record_fields(record, self.component, self.hostname)
        data["category"] = category
        data["msg"] = _clip(data["msg"])
        if "exc" in data:
            data["exc"] = _clip(data["exc"])
        timestamp = datetime.fromtimestamp(record.created, timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        body = json.dumps(data, ensure_ascii=False, default=str)
        return (
            f"{self._pri(record)}1 {timestamp} {_header_host(self.hostname)} "
            f"aifactory-{self.component} {record.process or '-'} {category} - {body}"
        )

    def _leef(self, record: logging.LogRecord, category: str) -> str:
        local = time.localtime(record.created)
        syslog_header = (
            f"{self._pri(record)}{_MONTHS[local.tm_mon - 1]} {local.tm_mday:>2} "
            f"{time.strftime('%H:%M:%S', local)} {_header_host(self.hostname)}"
        )
        header = "|".join(
            [
                "LEEF:2.0",
                _leef_header_field(LEEF_VENDOR),
                _leef_header_field(LEEF_PRODUCT),
                _leef_header_field(self.version),
                _leef_header_field(self.event_id(record, category)),
                LEEF_DELIMITER,
            ]
        ) + "|"
        attributes: list[tuple[str, object]] = [
            ("devTime", datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"
            )),
            ("devTimeFormat", LEEF_TIME_FORMAT),
            ("cat", category),
            ("sev", _leef_severity(record.levelno)),
            ("component", self.component),
            ("logger", record.name),
            ("level", record.levelname),
        ]
        request_id = getattr(record, "request_id", "")
        if request_id:
            attributes.append(("requestId", request_id))
        event = getattr(record, "event", None)
        if isinstance(event, dict):
            for key, value in event.items():
                if value in ("", None):
                    continue
                if key == "actor":
                    attributes.append(("usrName", value))
                elif key == "src_ip":
                    attributes.append(("src", value))
                elif key == "details" and isinstance(value, dict):
                    attributes.extend(
                        (_leef_key(f"detail_{name}"), item) for name, item in value.items()
                    )
                else:
                    attributes.append((_leef_key(key), value))
        if record.exc_info:
            attributes.append(("exc", logging.Formatter().formatException(record.exc_info)))
        # The message goes last so an over-long event loses only its tail.
        attributes.append(("msg", record.getMessage()))
        body = LEEF_DELIMITER.join(f"{key}={_leef_value(value)}" for key, value in attributes)
        return f"{syslog_header} {header}{body}"


def _tls_context(config: ForwardingConfig) -> ssl.SSLContext:
    context = ssl.create_default_context(cadata=config.ca_pem or None)
    if not config.tls_verify:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class _Connection:
    """One UDP socket or one TCP/TLS stream to the collector."""

    def __init__(self, config: ForwardingConfig):
        self.config = config
        self.sock: socket.socket | None = None
        self.address = None

    def open(self) -> None:
        config = self.config
        if config.protocol == "udp":
            family, kind, proto, _, address = socket.getaddrinfo(
                config.host, config.port, type=socket.SOCK_DGRAM
            )[0]
            self.sock = socket.socket(family, kind, proto)
            self.address = address
            return
        raw = socket.create_connection(
            (config.host, config.port), timeout=CONNECT_TIMEOUT_SECONDS
        )
        try:
            if config.protocol == "tls":
                raw = _tls_context(config).wrap_socket(raw, server_hostname=config.host)
            raw.settimeout(SEND_TIMEOUT_SECONDS)
        except BaseException:
            raw.close()
            raise
        self.sock = raw

    def send(self, payload: bytes) -> None:
        if self.sock is None:
            self.open()
        if self.config.protocol == "udp":
            self.sock.sendto(payload, self.address)
        else:
            # Non-transparent framing: one event per line.
            self.sock.sendall(payload + b"\n")

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:300]


class SyslogForwarder:
    """Bounded queue plus one sender thread for one collector."""

    def __init__(self, config: ForwardingConfig, *, queue_size: int = QUEUE_SIZE):
        self.config = config
        self._queue: collections.deque[bytes] = collections.deque()
        self._queue_size = queue_size
        self._condition = threading.Condition()
        self._stopping = False
        self._connection = _Connection(config)
        self.connected = False
        self.sent = 0
        self.dropped = 0
        self.last_error = ""
        self.last_error_at = ""
        self.last_sent_at = ""
        self._thread = threading.Thread(
            target=self._run, name="aifactory-log-forwarder", daemon=True
        )
        self._thread.start()

    def submit(self, payload: bytes) -> None:
        with self._condition:
            if self._stopping:
                return
            if len(self._queue) >= self._queue_size:
                self._queue.popleft()
                self.dropped += 1
            self._queue.append(payload)
            self._condition.notify()

    def _run(self) -> None:
        delay = 1.0
        while True:
            with self._condition:
                while not self._queue and not self._stopping:
                    self._condition.wait()
                if self._stopping and not self._queue:
                    break
                payload = self._queue[0]
            try:
                self._connection.send(payload)
            except Exception as exc:  # noqa: BLE001 - any failure means "retry later"
                self._connection.close()
                was_connected = self.connected
                self.connected = False
                self.last_error = _describe(exc)
                self.last_error_at = datetime.now(timezone.utc).isoformat()
                if was_connected or delay == 1.0:
                    logger.warning(
                        "Log forwarding to %s:%s/%s failed: %s",
                        self.config.host, self.config.port, self.config.protocol,
                        self.last_error,
                    )
                if self.config.protocol == "udp":
                    # A datagram that cannot be sent will not succeed later.
                    with self._condition:
                        if self._queue and self._queue[0] is payload:
                            self._queue.popleft()
                            self.dropped += 1
                with self._condition:
                    if self._stopping:
                        break
                    self._condition.wait(timeout=delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                continue
            if not self.connected and self.last_error:
                logger.info(
                    "Log forwarding to %s:%s/%s recovered",
                    self.config.host, self.config.port, self.config.protocol,
                )
            self.connected = True
            delay = 1.0
            with self._condition:
                if self._queue and self._queue[0] is payload:
                    self._queue.popleft()
                self.sent += 1
            self.last_sent_at = datetime.now(timezone.utc).isoformat()
        self._connection.close()

    def close(self, timeout: float = 2.0) -> None:
        """Stop after a short best-effort flush of queued records."""
        deadline = time.monotonic() + timeout
        while self._queue and self.connected and time.monotonic() < deadline:
            time.sleep(0.05)
        with self._condition:
            self._stopping = True
            self._queue.clear()
            self._condition.notify_all()
        self._thread.join(timeout=max(0.1, deadline - time.monotonic()))

    def status(self) -> dict:
        return {
            "enabled": True,
            "destination": f"{self.config.host}:{self.config.port}/{self.config.protocol}",
            "format": self.config.format,
            "connected": self.connected,
            "queued": len(self._queue),
            "sent": self.sent,
            "dropped": self.dropped,
            "last_error": self.last_error,
            "last_error_at": self.last_error_at,
            "last_sent_at": self.last_sent_at,
        }


class ForwardingHandler(logging.Handler):
    """Logging handler that filters by category and hands lines to a forwarder."""

    def __init__(self, forwarder: SyslogForwarder, formatter: SyslogFormatter):
        super().__init__()
        self.forwarder = forwarder
        self.syslog_formatter = formatter

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if not self.forwarder.config.accepts(record_category(record), record.levelno):
                return
            self.forwarder.submit(self.syslog_formatter.format(record))
        except Exception:  # noqa: BLE001 - never break the caller
            self.handleError(record)

    def update(self, config: ForwardingConfig) -> None:
        """Apply filter or format changes that keep the same connection."""
        self.forwarder.config = replace(config)
        self.syslog_formatter.config = self.forwarder.config

    def close(self) -> None:
        self.forwarder.close()
        super().close()


def send_test_event(
    config: ForwardingConfig, *, component: str, hostname: str, version: str, actor: str = ""
) -> dict:
    """Synchronously send one audit event with ``config``; for the admin test button."""
    record = logging.LogRecord(
        AUDIT_LOGGER, logging.INFO, __file__, 0,
        "AI Factory log forwarding test event", None, None,
    )
    record.event = {
        "action": "logging.test",
        "outcome": "success",
        "actor": actor,
    }
    payload = SyslogFormatter(
        config, component=component, hostname=hostname, version=version
    ).format(record)
    connection = _Connection(config)
    started = time.monotonic()
    try:
        connection.send(payload)
    except Exception as exc:  # noqa: BLE001 - reported to the admin
        return {
            "ok": False,
            "message": f"Gönderilemedi: {_describe(exc)}",
            "latency_ms": None,
        }
    finally:
        connection.close()
    latency = round((time.monotonic() - started) * 1000)
    if config.protocol == "udp":
        message = "Test olayı UDP ile gönderildi; UDP teslimatı doğrulanamaz, QRadar'da kontrol edin."
    else:
        message = "Test olayı gönderildi; QRadar'da 'logging.test' olayını kontrol edin."
    return {"ok": True, "message": message, "latency_ms": latency}
