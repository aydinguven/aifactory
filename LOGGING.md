# Logging, audit and forwarding

The controller and every worker write their own log files and can forward
records to a syslog collector such as IBM QRadar. Both are configured under
**Admin > Günlükler**. Host rsyslog, logrotate and journald configuration is
not touched, so centrally managed host logging keeps working unchanged.

## Local files

```
/var/log/aifactory/                root:aifactory 0750
  controller/                      controller (UID 10001 when containerized)
    app.log                        application and library records
    audit.log                      audit events
    access.log                     one line per HTTP request
  worker/                          worker agent
    app.log
    audit.log
  updater/                         output of each queued platform update
    update-YYYYMMDD-HHMMSS.log
```

Each line is one JSON object:

```json
{"ts": "2026-11-11T09:12:03.120Z", "level": "WARNING", "component": "controller",
 "host": "aif-ctl-01", "logger": "aifactory.audit", "msg": "auth.login failure actor=jdoe target=-",
 "request_id": "4f0c…", "event": {"action": "auth.login", "outcome": "failure",
 "actor": "jdoe", "src_ip": "10.1.2.3", "details": {"reason": "invalid_credentials"}}}
```

The installer creates these directories. Processes write files only into a
directory that already exists, so development and test runs do not write to
`/var/log`. The container units also create their directory before start,
because their bind mounts need it.

The containerized controller mounts its directory with `:Z`, so SELinux labels
it `container_file_t`. Root shells can read it. A confined host agent such as
rsyslog `imfile` cannot read it without a local policy change; use the built-in
forwarding instead.

### Rotation and retention

- A file rotates at local midnight, or earlier once it reaches a tenth of the
  size cap (between 1 MB and 256 MB). The rotated file is renamed to
  `app.log.YYYYMMDD-HHMMSS` and gzipped.
- Rotated files older than the retention period are deleted. The default is
  14 days.
- If a component directory grows past the size cap (default 1024 MB), the
  oldest rotated files are deleted first. Audit files are deleted last.
- The updater keeps the newest 30 run logs.

### Levels

The application level (default INFO) applies to `aifactory.*` and Uvicorn.
Libraries such as httpx and SQLAlchemy log only warnings unless the level is
DEBUG. Audit and access records are always written.

Before the admin policy exists, `LOG_LEVEL` in `controller.env` or
`worker.env` sets the level. If it is empty, a controller with `DEBUG=True`
logs at DEBUG and everything else logs at INFO.

Once file logging is active, journald receives only WARNING and above, so
`journalctl -u aifactory-controller` stays useful for failures and the
journal does not hold a second full copy. Set `LOG_CONSOLE_LEVEL` to change
this.

## Audit events

| Action | When |
|---|---|
| `auth.login` | Every login attempt. Failures carry `reason`: `invalid_credentials`, `directory_rejected` or `directory_unavailable`. |
| `auth.logout`, `auth.register` | Logout and self-registration. |
| `METHOD /route/template` | Every other `POST`, `PUT`, `PATCH` or `DELETE` under `/api/`, for example `PUT /api/admin/users/{user_id}/quota`. `fields` lists the top-level JSON field names of the request body, never the values. |
| `logging.settings.update` | Admin changed the logging policy; `changed` lists the fields. |
| `worker.connect`, `worker.disconnect` | A worker tunnel opened or closed. |
| `worker.auth` | A request with an unknown, disabled or wrong worker token (outcome `denied`). |
| `logging.settings.applied` | A worker applied a logging policy revision, at start (cached policy) or after a change. This is the first event QRadar receives from each worker. |

`outcome` is `success` (HTTP < 400), `denied` (401 or 403) or `failure`.
Failures and denials are logged as WARNING. Worker-token API calls and model
inference requests are not audited; they appear in `access.log`.

## Forwarding

Each process sends directly to the collector, so QRadar sees the real source
host. Workers need a network path to the collector.

| Setting | Notes |
|---|---|
| Protocol | UDP, TCP, or TCP with TLS. TCP uses one event per line. |
| Port | Usually 514 for UDP/TCP and 6514 for TLS. |
| Format | **LEEF 2.0** (default) with an RFC 3164 syslog header, or **RFC 5424** with the JSON line above as the message. |
| TLS | Verification can be turned off for testing. The optional CA bundle replaces the system trust store. Client certificates are not supported. |
| Records | Audit events, application records at or above a chosen level (default WARNING), and HTTP access records (off by default, high volume). |

Records wait in an in-memory queue of 10,000 while the collector is down. When
the queue is full, the oldest record is dropped and counted. The sender retries
with backoff up to 60 seconds. Local files are written either way, so
`audit.log` remains the complete record. A single event is cut to 8 KB.

LEEF events use vendor `AIFactory`, product `AI Factory` and the platform
version. The event ID is the audit action, `http.request` for access records,
or `<logger>.<level>` for application records. Attributes include `devTime`,
`devTimeFormat`, `cat` (`audit`, `app`, `access`), `sev` (1-10), `usrName`,
`src`, `component`, `requestId` and `msg`. In QRadar, add a log source of type
**Universal LEEF** for each controller and worker host, or let auto-discovery
create them.

**Test Olayı Gönder** sends one `logging.test` audit event from the controller
using the unsaved form values. With UDP the controller cannot confirm delivery,
so check QRadar.

### How workers get the policy

Saving applies the policy on the controller immediately. Workers fetch
`/api/agent/logging-settings` every 30 seconds and when they reconnect, and
cache the last policy in the workspace root so forwarding resumes after a
restart. The status table on the admin page shows each worker's applied
revision, file state, and forwarding counters from its latest heartbeat.
A forwarder connects with its first record, so a node that has sent nothing
yet shows **Bekliyor**. UDP nodes show **Gönderiyor** instead of **Bağlı**,
because UDP delivery cannot be confirmed.

## Request IDs

Every HTTP response carries an `X-Request-ID` header. A valid incoming
`X-Request-ID` (up to 64 letters, digits, `.`, `_`, `-`) is reused. All
records written while handling the request include it as `request_id`.
