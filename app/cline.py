"""Cline configuration generated from the controller-managed AI gateway."""

from __future__ import annotations

import json
from typing import Any


def managed_vscode_settings() -> str:
    """Use pinned Cline without native Chat or remote bundle rollouts."""
    return json.dumps(
        {
            "extensions.autoCheckUpdates": False,
            "extensions.autoUpdate": False,
            "chat.disableAIFeatures": True,
            "cline.rollout.bundleOverride": "legacy",
            # Same as Kilo Code workspaces: forward only ports whose URL is
            # printed, and no telemetry to hosts workspaces cannot reach.
            "remote.autoForwardPortsSource": "output",
            "telemetry.telemetryLevel": "off",
        },
        separators=(",", ":"),
    )


def openai_compatible_base_url(gateway_url: str) -> str:
    """Return the OpenAI-compatible /v1 URL for a gateway root URL."""
    root = str(gateway_url or "").strip().rstrip("/")
    if not root:
        return ""
    if root.endswith("/v1"):
        return root
    return f"{root}/v1"


def managed_cline_files(
    gateway_url: str,
    api_key: str,
    model_id: str,
) -> dict[str, str]:
    """Build legacy and SDK Cline state files for one managed profile.

    Cline's stable VS Code surface still consumes the file-backed legacy state,
    while the newer SDK-backed surface consumes providers.json. Shipping both
    formats keeps managed workspaces compatible across Cline rollouts.
    """
    base_url = openai_compatible_base_url(gateway_url)
    model_id = str(model_id or "").strip()
    api_key = str(api_key or "")
    if not base_url or not api_key or not model_id:
        return {}

    global_state: dict[str, Any] = {
        "isNewUser": False,
        "welcomeViewCompleted": True,
        "mode": "act",
        "planModeApiProvider": "openai",
        "actModeApiProvider": "openai",
        "planModeApiModelId": model_id,
        "actModeApiModelId": model_id,
        "planModeOpenAiModelId": model_id,
        "actModeOpenAiModelId": model_id,
        "openAiBaseUrl": base_url,
    }
    secrets = {"openAiApiKey": api_key}
    providers = {
        "version": 1,
        "lastUsedProvider": "openai-compatible",
        "providers": {
            "openai-compatible": {
                "settings": {
                    "provider": "openai-compatible",
                    "apiKey": api_key,
                    "model": model_id,
                    "baseUrl": base_url,
                },
                "tokenSource": "manual",
            }
        },
    }
    compact = {"ensure_ascii": False, "separators": (",", ":")}
    return {
        "globalState.json": json.dumps(global_state, **compact),
        "secrets.json": json.dumps(secrets, **compact),
        "settings/providers.json": json.dumps(providers, **compact),
    }
