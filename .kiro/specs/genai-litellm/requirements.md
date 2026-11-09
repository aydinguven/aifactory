# GenAI (LiteLLM) — Requirements

## Context

AI Factory users need a personal LiteLLM API key without asking an admin.
LiteLLM runs at `http://idmvopenuit1:5003` (UI at `/ui/`). It is the open-source
edition, so team features are not used. AI Factory talks to it through an
`aifactory` LiteLLM user that has the `proxy_admin` role.

## Requirements

### R1 — Admin configuration
1. In Yönetim → Entegrasyonlar, an admin can set: enabled, LiteLLM base URL,
   admin API key (the `aifactory` key), TLS verification, CA file and timeout.
2. The admin key is stored encrypted and is never returned by any API. The API
   only reports `has_admin_key`.
3. Saving with `admin_key: null` keeps the current key.
4. A "Test" button checks that the URL works and that the key has admin rights.
5. Optional settings sent with every new user/key (model list, max budget,
   budget duration, key duration, user role) can be left empty. Empty means
   LiteLLM's own defaults apply.

### R2 — GenAI tab
1. Every logged-in user sees a **GenAI** nav tab after MLflow.
2. If GenAI is disabled or not configured, the tab shows a short
   "not configured yet" message.
3. Otherwise the page shows one of these states:
   - **No access**: a "GenAI erişimi oluştur" button.
   - **Active**: LiteLLM user id, key alias, creation/rotation date, and usage.
   - **Existing LiteLLM user without an AI Factory key** (created by hand): a
     "generate key" button that reuses that LiteLLM user.
   - **LiteLLM unreachable**: an error message. The page itself still renders.

### R3 — Provisioning
1. The button creates a LiteLLM user with `user_id` set to the AI Factory
   username in lowercase (e.g. `k015570`), plus the user's email.
2. It then generates a **personal key** owned by that user and shows it
   **once**, with a copy button, the base URL and a usage snippet.
3. The personal key is never stored in plaintext or logged. Only its LiteLLM
   token id/hash and alias are stored.
4. Running it again (double click, retry) is safe. If the LiteLLM user already
   exists, AI Factory reuses it.
5. All LiteLLM calls are made by the controller. The admin key never reaches
   the browser.

### R4 — Rotation
1. The user can rotate the personal key. This creates a new key, shows it
   once, and deletes the old one.
2. Usage history stays the same after rotation, because LiteLLM tracks spend
   per `user_id`.

### R5 — Usage
1. The page shows the user's total spend, and the budget if one is set, from
   `/user/info`.
2. If the LiteLLM version supports it, the page also shows a daily breakdown
   for the last 30 days. Otherwise this section is hidden.

### R6 — Per-user workspace key (phase 2)
1. AI Factory also creates a separate **workspace key**
   (`devcloud-<username>-workspace`) for the same LiteLLM user. It is stored
   encrypted and never shown to the user.
2. New Jupyter/VS Code workspaces of a provisioned user get this key instead of
   the shared Jupyter AI token. This applies to `ANTHROPIC_AUTH_TOKEN` and to
   the Cline config. Users who are not provisioned keep the shared token.
3. Usage from workspaces is therefore counted under the user's LiteLLM spend.
4. Workers that don't know the new field still use the shared token.

### Non-functional
- Turkish UI texts, matching the existing pages.
- Responses that contain a key use `Cache-Control: no-store`.
- Usernames are lowercased before they are sent to LiteLLM.
- Error details from LiteLLM are cleaned up and truncated to 500 characters, as
  in the MLflow client.
- Works on both SQLite and PostgreSQL.
