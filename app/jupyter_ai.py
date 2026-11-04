"""Shared Jupyter AI model-catalog compatibility helpers."""

from __future__ import annotations

import json
import re


# Workers older than this drop ``ai_ca_bundle`` from container.create and
# ignore it in the connectivity test.
CA_BUNDLE_MIN_AGENT_VERSION = "3.26.0"
MAX_CA_BUNDLE_BYTES = 256 * 1024
# Only base64 text between the markers, so the bundle can never carry
# anything but certificates into a container environment.
_CERTIFICATE_BLOCK = re.compile(
    r"-----BEGIN CERTIFICATE-----[A-Za-z0-9+/=\s]+?-----END CERTIFICATE-----"
)


def normalize_ca_bundle(value: object) -> str:
    """Return the PEM certificate blocks of ``value`` in canonical form, else ''.

    The controller additionally parses every certificate before storing it;
    this check is the cheap structural gate the worker also applies.
    """
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text.encode("utf-8")) > MAX_CA_BUNDLE_BYTES:
        return ""
    blocks = _CERTIFICATE_BLOCK.findall(text)
    if not blocks or _CERTIFICATE_BLOCK.sub("", text).strip():
        return ""
    return "\n".join(
        "\n".join(line.strip() for line in block.splitlines() if line.strip())
        for block in blocks
    ) + "\n"


DEFAULT_JUPYTER_AI_MODELS = (
    {
        "model_id": "qwen3.6-35b",
        "name": "Qwen 3.6 35B (On-Prem)",
        "description": "On-Prem",
    },
    {
        "model_id": "openrouter/z-ai/glm-5.2",
        "name": "GLM 5.2 (Online)",
        "description": "Online",
    },
    {
        "model_id": "openrouter/deepseek/deepseek-v4-pro",
        "name": "DeepSeek V4 Pro (Online)",
        "description": "Online",
    },
    {
        "model_id": "openrouter/qwen/qwen3-coder",
        "name": "Qwen 3 Coder (Online)",
        "description": "Online",
    },
    {
        "model_id": "openrouter/moonshotai/kimi-k2.6",
        "name": "Kimi k2.6 (Online)",
        "description": "Online",
    },
)


def default_model_catalog() -> list[dict[str, str]]:
    return [dict(model) for model in DEFAULT_JUPYTER_AI_MODELS]


def default_model_catalog_json() -> str:
    return json.dumps(default_model_catalog(), ensure_ascii=False)


def parse_model_catalog(raw: str, default_model: str = "") -> list[dict[str, str]]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        value = []
    models: list[dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            model_id = str(item.get("model_id") or "").strip()
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            models.append(
                {
                    "model_id": model_id,
                    "name": str(item.get("name") or model_id).strip() or model_id,
                    "description": str(item.get("description") or "").strip(),
                }
            )
    if default_model and default_model not in seen:
        models.insert(
            0,
            {
                "model_id": default_model,
                "name": default_model,
                "description": "Default",
            },
        )
    return models


def model_environment(
    default_model: str,
    models: list[dict[str, str]],
    *,
    discovery_enabled: bool,
) -> dict[str, str]:
    """Translate the central catalog into Claude Code gateway configuration."""
    catalog = parse_model_catalog(json.dumps(models), default_model)
    if not default_model or not catalog:
        return {
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": (
                "1" if discovery_enabled else "0"
            )
        }

    environment = {
        "ANTHROPIC_MODEL": default_model,
        "ANTHROPIC_DEFAULT_MODEL": default_model,
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": (
            "1" if discovery_enabled else "0"
        ),
    }
    # Claude Code's built-in aliases can be used by subagents even when the
    # main session is running against a custom gateway model. Keep every
    # family pinned to the known-good central default. Arbitrary catalog
    # entries are exposed separately through the supported modelPicker setting.
    aliases = (
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_FABLE_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    )
    default_entry = next(
        item for item in catalog if item["model_id"] == default_model
    )
    for prefix in aliases:
        environment[prefix] = default_model
        environment[f"{prefix}_NAME"] = default_entry["name"]
        environment[f"{prefix}_DESCRIPTION"] = default_entry["description"]
        environment[f"{prefix}_SUPPORTED_CAPABILITIES"] = "none"
    return environment


def claude_settings(
    default_model: str,
    models: list[dict[str, str]],
) -> dict[str, object]:
    """Build Claude Code settings for an exact, multi-model gateway picker."""
    catalog = parse_model_catalog(json.dumps(models), default_model)
    if not default_model or not catalog:
        return {}

    return {
        "model": default_model,
        "availableModels": [item["model_id"] for item in catalog],
        "enforceAvailableModels": True,
        "modelPicker": {
            "mode": "replace",
            "options": [
                {
                    "model": item["model_id"],
                    "label": item["name"],
                    "description": item["description"],
                }
                for item in catalog
            ],
        },
    }
