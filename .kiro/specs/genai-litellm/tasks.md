# GenAI (LiteLLM) — Tasks

## Phase 1 — GenAI tab and personal key

- [x] 1. Models and migration: `GenAiSettings` and `GenAiAccount`, registered in `models/__init__.py` and `database.init_db`, migration v26.
- [ ] 2. Env-var fallback (`GENAI_LITELLM_URL`, `GENAI_LITELLM_ADMIN_KEY`). Deferred: the admin UI is enough for now.
- [x] 3. `app/integrations/litellm.py` client. Secrets are removed from error messages.
- [x] 4. Provisioning service (`app/genai.py`): status, provisioning (safe to retry, adopts an existing LiteLLM user, cleans up the key if the commit fails), rotation.
- [x] 5. `/api/genai` routes: `GET`/`POST /account`, `POST /account/rotate`, `GET /usage`.
- [x] 6. Admin `GET`/`PUT /api/admin/genai-settings` and `POST /api/admin/genai-settings/test`, the form in `admin.html`, the handler in `app.js`.
- [x] 7. `/genai` page: nav link, view, `genai.html`, `genai.js`.
- [x] 8. Tests in `tests/test_genai.py` (fake LiteLLM through `httpx.MockTransport`).
- [x] 9. README GenAI section.

Changes from the design:
- The budget (`max_budget`, `budget_duration`) is set on the LiteLLM **user**, not on the key. That way it covers rotated keys and the phase 2 workspace key, and rotating a key cannot reset the budget.
- The admin test reads the owner from `/key/info`, then that owner's role from `/user/info`, instead of calling `/user/list`.

## Phase 2 — per-user workspace key

- [x] 10. `ensure_workspace_key(user)` plus the columns `workspace_key_alias`, `encrypted_workspace_key`, `workspace_key_token` (migration v27).
- [x] 11. Pass `ai_gateway_token` at the three `create_workspace_container` call sites in `workspace_routes.py`, falling back to the shared token if anything fails.
- [x] 12. Worker: add the field to the allowed list, and use it in `podman_service` for `ANTHROPIC_AUTH_TOKEN` and the Cline config.
- [x] 13. Tests: env injection with and without the key, the fallback when LiteLLM is down, and the old-worker case.

Phase 2 notes (3.12.0):
- Only users who created GenAI access get a workspace key. Everyone else keeps the shared key, and the shared key stays as the fallback. (Since 2026-11-06, AD users get GenAI access, a LiteLLM user and a workspace key, automatically at login.)
- LiteLLM teams: a default team for new users, and a priority list that decides which team each key binds to. The workspace key is reissued when the user's team changes.
- A per-user key is injected only when the GenAI base URL and the Workspace AI gateway URL point to the same LiteLLM.
