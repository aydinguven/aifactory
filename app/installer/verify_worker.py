"""Verify that an installed worker is authenticated and connected."""

from __future__ import annotations

import os
import ssl
import time

import httpx


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required for worker verification")
    return value


def main() -> int:
    controller = (
        os.environ.get("AIFACTORY_CONTROLLER_URL", "").strip()
        or _required("AIFACTORY_MASTER_URL")
    ).rstrip("/")
    node_id = _required("AIFACTORY_NODE_ID")
    token = _required("AIFACTORY_NODE_TOKEN")
    ca_file = os.environ.get("AIFACTORY_AGENT_CA_FILE", "").strip()
    cert_file = os.environ.get("AIFACTORY_AGENT_CERT_FILE", "").strip()
    key_file = os.environ.get("AIFACTORY_AGENT_KEY_FILE", "").strip()
    if bool(cert_file) != bool(key_file):
        raise RuntimeError("Worker client certificate and key must be configured together")
    # Match app.worker_agent: trust the host store (update-ca-trust) or the
    # configured CA file, not httpx's bundled certifi list.
    verify = ssl.create_default_context(cafile=ca_file or None)
    if cert_file:
        verify.load_cert_chain(certfile=cert_file, keyfile=key_file)
    deadline = time.monotonic() + 45
    last_error = "controller did not report the worker connected"
    with httpx.Client(
        timeout=3.0,
        verify=verify,
    ) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(
                    f"{controller}/api/agent/check",
                    params={"node_id": node_id},
                    headers={"Authorization": f"Bearer {token}"},
                )
                if response.status_code == 403:
                    raise RuntimeError(
                        "Controller rejected the worker ID or enrollment token"
                    )
                response.raise_for_status()
                if response.json().get("connected") is True:
                    print(f"Worker {node_id} is authenticated and connected.")
                    return 0
                last_error = "credentials accepted; waiting for the worker tunnel"
            except RuntimeError:
                raise
            except Exception as exc:
                last_error = str(exc)
            time.sleep(1)
    raise RuntimeError(
        f"Worker enrollment verification timed out: {last_error}"
    )


if __name__ == "__main__":
    raise SystemExit(main())
