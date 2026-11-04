"""Kilo Code configuration generated from the controller-managed AI gateway.

The Kilo Code extension and its bundled CLI both read the global config file
``~/.config/kilo/kilo.jsonc``. The file written into a workspace holds:

* the workspace owner's own AI Factory (LiteLLM) key, for chat requests;
* the shared indexing key from Admin, for codebase-indexing embeddings;
* the catalog MCP servers and skills URLs from Admin, with the optional shared
  catalog token.

Neither a key nor any internal host name is baked into the image.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.cline import openai_compatible_base_url

KILO_CODE_ASSISTANT = "kilo-code"
# First worker release that accepts kilo_code_config and knows the vscode-kilo
# template; older workers silently start the image with no kilo.jsonc.
MIN_AGENT_VERSION = "3.24.0"
EXTENSION_ID = "kilocode.kilo-code"
CONFIG_SCHEMA = "https://app.kilo.ai/config.json"
CONFIG_DIR = "/home/coder/.config/kilo"
CONFIG_FILENAME = "kilo.jsonc"
PROVIDER_ID = "litellm"
PROVIDER_NAME = "TCMB code"
# Shown in Kilo Code's model picker when the owner has no AI Factory key yet.
MISSING_KEY_PROVIDER_NAME = (
    "TCMB code (anahtar yok: GenAI sekmesinden AI Factory anahtarı talep edin)"
)
REASONING_EFFORT = "high"
# Kilo's own cloud gateway is unreachable from workspaces.
DISABLED_PROVIDERS = ["kilo"]
# Project rules installed by the catalog (LocalMCP.sh writes .kilo/rules/*.md).
INSTRUCTIONS = [".kilo/rules/*.md"]
# Baked into the vscode-kilo and vscode-cuda images.
LOCAL_MCP_SCRIPT = "/opt/kilo/LocalMCP.sh"
CATALOG_MCP_SERVER = "kilo-catalog"
INSTALL_MCP_SERVER = "kilo-install"
MAX_CONFIG_LENGTH = 65536
CONFIG_DIGEST_SALT = b"devcloud-kilo-code-config-v1\x00"
DEFAULT_INDEXING_MODEL = "bge-m3"
DEFAULT_INDEXING_DIMENSION = 1024


def parse_model_ids(raw: str | None) -> list[str]:
    """Return the stored Kilo Code model allow-list; empty means the whole catalog."""
    return _parse_string_list(raw)


def parse_skills_urls(raw: str | None) -> list[str]:
    """Return the stored skills URLs."""
    return _parse_string_list(raw)


def _parse_string_list(raw: str | None) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if item and item not in result:
            result.append(item)
    return result


def select_models(
    catalog: list[dict[str, str]],
    model_ids: list[str],
    default_model: str,
) -> tuple[list[dict[str, str]], str]:
    """Pick the catalog models offered in Kilo Code and its default model."""
    models = [item for item in catalog if not model_ids or item["model_id"] in model_ids]
    if not models:
        models = list(catalog)
    if not models:
        return [], ""
    ids = [item["model_id"] for item in models]
    return models, default_model if default_model in ids else ids[0]


def catalog_mcp_servers(catalog_url: str, catalog_token: str = "") -> dict[str, Any]:
    """The remote catalog MCP server and the local installer that writes into .kilo."""
    catalog_url = catalog_url.rstrip("/")
    if not catalog_url:
        return {}
    remote: dict[str, Any] = {
        "type": "remote",
        "url": f"{catalog_url}/mcp",
        "oauth": False,
    }
    environment = {"CATALOG_URL": catalog_url}
    if catalog_token:
        remote["headers"] = {"Authorization": f"Bearer {catalog_token}"}
        environment["MCP_TOKEN"] = catalog_token
    return {
        CATALOG_MCP_SERVER: remote,
        INSTALL_MCP_SERVER: {
            "type": "local",
            "command": ["bash", LOCAL_MCP_SCRIPT],
            "environment": environment,
        },
    }


def render_config(
    *,
    gateway_url: str,
    models: list[dict[str, str]],
    default_model: str,
    user_key: str,
    catalog_url: str = "",
    catalog_token: str = "",
    skills_urls: list[str] | None = None,
    skills_paths: list[str] | None = None,
    indexing_enabled: bool = False,
    indexing_model: str = DEFAULT_INDEXING_MODEL,
    indexing_dimension: int = DEFAULT_INDEXING_DIMENSION,
    indexing_key: str = "",
) -> str:
    """Build the global kilo.jsonc content as deterministic JSON (valid JSONC)."""
    base_url = openai_compatible_base_url(gateway_url)
    if not base_url or not models or not default_model:
        return ""
    options: dict[str, Any] = {"baseURL": base_url, "reasoningEffort": REASONING_EFFORT}
    if user_key:
        options["apiKey"] = user_key
    config: dict[str, Any] = {
        "$schema": CONFIG_SCHEMA,
        "disabled_providers": list(DISABLED_PROVIDERS),
        "instructions": list(INSTRUCTIONS),
        "model": f"{PROVIDER_ID}/{default_model}",
        "provider": {
            PROVIDER_ID: {
                "name": PROVIDER_NAME if user_key else MISSING_KEY_PROVIDER_NAME,
                "npm": "@ai-sdk/openai-compatible",
                "options": options,
                "models": {
                    item["model_id"]: {
                        "name": item.get("name") or item["model_id"],
                        "reasoning": True,
                    }
                    for item in models
                },
            }
        },
    }
    servers = catalog_mcp_servers(catalog_url, catalog_token)
    if servers:
        config["mcp"] = {"servers": servers}
    # One list takes local folders and URLs; a folder declared in this
    # user-level config is trusted (the Admin skill library lives there).
    skills = [*(skills_paths or []), *(skills_urls or [])]
    if skills:
        config["skills"] = skills
    if indexing_enabled and indexing_key and indexing_model:
        config["indexing"] = {
            "provider": "openai-compatible",
            "openai-compatible": {"baseUrl": base_url, "apiKey": indexing_key},
            "dimension": int(indexing_dimension),
            "model": indexing_model,
            "vectorStore": "lancedb",
            "enabled": True,
        }
    return json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validated_config(raw: Any) -> str:
    """Return a controller-rendered config only when it is a bounded JSON object."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_CONFIG_LENGTH:
        return ""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return ""
    return raw if isinstance(parsed, dict) else ""


def config_digest(config: str) -> str:
    """Fingerprint of the rendered config; changes when a key or setting changes.

    Salted with a format version so a format change rebuilds every container
    once on its next start.
    """
    if not config:
        return ""
    return hashlib.sha256(
        CONFIG_DIGEST_SALT + config.encode("utf-8")
    ).hexdigest()


def managed_vscode_settings(config: str = "") -> str:
    """VS Code user settings: no auto-updates, no native Chat, our default model."""
    values: dict[str, Any] = {
        "extensions.autoCheckUpdates": False,
        "extensions.autoUpdate": False,
        "chat.disableAIFeatures": True,
    }
    model = ""
    if config:
        try:
            model = str(json.loads(config).get("model") or "")
        except (ValueError, AttributeError):
            model = ""
    provider_id, separator, model_id = model.partition("/")
    if separator and provider_id and model_id:
        # Without these the extension starts on Kilo's own cloud default model.
        values["kilo-code.new.model.providerID"] = provider_id
        values["kilo-code.new.model.modelID"] = model_id
    return json.dumps(values, separators=(",", ":"))
