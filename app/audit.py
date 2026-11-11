"""Audit events: who did what, from where, and whether it succeeded.

Records go to the ``aifactory.audit`` logger, which logging_setup writes to
``audit.log`` and can forward to QRadar. Never pass secret values here;
detail keys that look like credentials are dropped as a safeguard.
"""

from __future__ import annotations

import logging

from app.log_forwarding import AUDIT_LOGGER

audit_logger = logging.getLogger(AUDIT_LOGGER)

# Set on the ASGI scope by routes that record their own audit event, so
# RequestLoggingMiddleware does not add a generic one for the same request.
AUDITED_SCOPE_KEY = "aifactory.audited"

_SECRET_MARKERS = ("password", "secret", "token", "key", "credential", "private")


def _safe_details(details: dict) -> dict:
    return {
        name: value
        for name, value in details.items()
        if value not in (None, "", [], {})
        and not any(marker in name.lower() for marker in _SECRET_MARKERS)
    }


def audit_event(
    action: str,
    *,
    outcome: str = "success",
    actor: str = "",
    actor_role: str = "",
    src_ip: str = "",
    target: str = "",
    **details,
) -> None:
    """Record one audit event; failures and denials are logged as warnings."""
    event = {
        "action": action,
        "outcome": outcome,
        "actor": actor,
        "actor_role": actor_role,
        "src_ip": src_ip,
        "target": target,
    }
    event = {key: value for key, value in event.items() if value}
    safe = _safe_details(details)
    if safe:
        event["details"] = safe
    level = logging.INFO if outcome == "success" else logging.WARNING
    audit_logger.log(
        level,
        "%s %s actor=%s target=%s",
        action,
        outcome,
        actor or "-",
        target or "-",
        extra={"event": event},
    )


def client_ip(connection) -> str:
    """Peer address after Uvicorn's trusted proxy-header handling."""
    client = getattr(connection, "client", None)
    return client.host if client else ""


def mark_audited(request) -> None:
    request.scope[AUDITED_SCOPE_KEY] = True
