# GenAI (LiteLLM) — Design

Follows the MLflow and Jupyter AI integration patterns: an admin settings
singleton, an async httpx client, and Fernet-encrypted secrets
(`app/security/secrets.py`).

## LiteLLM API used (checked against litellm 1.83.x source)

All calls send `Authorization: Bearer <aifactory admin key>`.

| Purpose | Call |
|---|---|
| Test / admin check | `GET /key/info` (no args → owner of the calling key), then `GET /user/info?user_id=<owner>` → `user_role` must be `proxy_admin` (an ownerless master key counts as admin) |
| Lookup user | `GET /user/info?user_id=<u>` → 404 if missing (older versions return 200 with `user_info: null`; treat both as missing) |
| Create user | `POST /user/new` `{user_id, user_email, user_role?, auto_create_key: false, metadata: {source: "devcloud"}}` |
| Create key | `POST /key/generate` `{user_id, key_alias, models?, max_budget?, budget_duration?, duration?, metadata: {source: "devcloud", purpose: "personal"\|"workspace"}}` → `key`, `token_id`/`token` |
| Delete key | `POST /key/delete` `{keys: [<token hash>]}` |
| Daily usage | `GET /user/daily/activity?start_date&end_date` (optional; if it fails, the section is hidden) |

If `/user/new` reports that the user already exists, AI Factory adopts that user.
Key aliases include a timestamp (`devcloud-k015570-202610061530`) so a new key
never clashes with an old alias.

## Data model (migration v26)

`genai_settings` (singleton, `id=1`):
`enabled`, `base_url`, `encrypted_admin_key`, `validate_tls`, `ca_cert_file`,
`timeout_seconds=15`, `user_role` (nullable), `allowed_models_json` (nullable),
`max_budget` (nullable float), `budget_duration`, `key_duration`, `updated_at`.

`genai_accounts` (one row per AI Factory user):
`id`, `user_id` FK→users (unique, cascade), `litellm_user_id`,
`personal_key_alias`, `personal_key_token` (hash, never the key),
`workspace_key_alias`, `encrypted_workspace_key`, `workspace_key_token`,
`created_at`, `rotated_at`.

The tables are created by `init_db`. The v26 step only records the version.

## Components

- `app/integrations/litellm.py`: `LiteLLMConfig`, `config_from_record`,
  `validate_config`, `LiteLLMClient` (`get_user`, `create_user`,
  `generate_key`, `delete_key`, `whoami`, `daily_activity`), and the errors
  `LiteLLMConfigurationError`, `LiteLLMConnectionError`, `LiteLLMNotFound`.
  This mirrors `app/integrations/mlflow.py`.
- `app/services/genai.py` (or inline in routes): `get_status(user)`,
  `provision(user)`, `rotate_personal_key(user)`, `ensure_workspace_key(user)`.
  Provisioning runs under a per-user `asyncio.Lock`, and the unique
  `genai_accounts.user_id` constraint protects against races between
  processes. Order: lookup/create the LiteLLM user → generate the key →
  commit the row. If the commit fails, the new key is deleted.
- `app/routes/genai_routes.py` (`/api/genai`):
  - `GET /account` → `{configured, provisioned, litellm_user_exists, user_id, key_alias, created_at, rotated_at, usage, error?}`
  - `POST /account` → `{api_key, base_url, key_alias}` (the key is in this response only, with `no-store`)
  - `POST /account/rotate` → same shape as `POST /account`
- Admin endpoints: `GET`/`PUT /api/admin/genai-settings` and
  `POST /api/admin/genai-settings/test` in `admin_routes.py`, plus a form in
  the `integrations` section of `admin.html` and handlers in `app.js`. If the
  base URL is empty, it is pre-filled from `JupyterAiSettings.gateway_url`.
- UI: a nav link in `base.html`, `GET /genai` in `view_routes.py`,
  `templates/genai.html`, and `static/js/genai.js`. The page shows the key in a
  read-only input with a copy button and a "this will not be shown again"
  warning. It also shows an OpenAI SDK/curl snippet with
  `base_url=<base>/v1`.
- Config fallback: `GENAI_LITELLM_URL` and `GENAI_LITELLM_ADMIN_KEY` in
  `config.py` and `.env.example`. Settings saved in the DB take precedence.

## Phase 2 — workspace key injection

1. On workspace create (`workspace_routes.py`: three `create_workspace_container`
   call sites), if GenAI is enabled and the owner is provisioned, call
   `ensure_workspace_key(owner)` and pass `ai_gateway_token=<key>` in the
   kwargs.
2. The worker adds `ai_gateway_token` to the allowed `container.create` keys
   (`worker_agent.py` ~line 650). `podman_service.create_workspace_container`
   uses it instead of `settings.JUPYTER_AI_GATEWAY_TOKEN` for
   `ANTHROPIC_AUTH_TOKEN` and `managed_cline_files(...)`.
3. Old workers drop the unknown field and keep using the shared token, so a
   rolling upgrade is safe.
4. The key is baked into the container env. After a workspace key changes,
   only newly created or recreated workspaces pick it up.
5. If LiteLLM is unreachable, workspace creation does not fail. It falls back
   to the shared token and logs a warning.

## Security notes

- The admin key is encrypted at rest and never serialized. A test asserts it
  never appears in any response.
- The personal key is never persisted. The workspace key is encrypted at rest,
  like other secrets. Rotating `SECRET_KEY` makes the stored keys unreadable;
  in that case `ensure_workspace_key` generates a new key.
- Lowercase usernames are validated against a strict character set before
  they are sent to LiteLLM.
