# AI Factory installer and lifecycle guide

The supported production target is Rocky Linux or RHEL 9.x/10.x on x86_64.
EL9 hosts run AI Factory under the AppStream `python3.12` interpreter (the
platform `python3` is 3.9); setup installs `python3.12` and `python3.12-pip`
from the enabled repositories when missing. Run the installer as root; operating-system packages come from the VM's enabled
DNF repositories, including Satellite or Foreman-managed repositories.

## Start the interactive installer

From a verified checkout or extracted release:

    sudo bash deploy/aifactory-setup.sh

The menu provides controller, all-in-one, and worker installation plus update,
repair, status, backup, restore, and uninstall.

The shell bootstrap validates the OS, architecture, root privileges, DNF, and
systemd. If subscription-manager is absent, it installs it from configured
repositories. If those repositories cannot provide it, a matching
offline/system-rpms/distribution-10-x86_64 closure is used when present. The
installer does not register a subscription or activation key.

When an offline artifact manifest is present, the behavior is stricter: the
installer verifies and installs the bundled RPM closure with every repository
disabled before it starts the Python UI. It then verifies the complete artifact
manifest and installs wheels without an index. Both bundle roles include the
immutable rootful worker image; the server bundle also includes controller and
PostgreSQL images. Base bundles do not contain workspace images. It never
falls back to a connected repository for that installation.

## Controller

Choose SQLite for the smallest footprint, bundled PostgreSQL for a controller
expected to manage several workers, or an external PostgreSQL URL for an
operator-managed database.

The recommended controller runtime is an immutable OCI image supervised by
Podman Quadlet. The optional bundled PostgreSQL database is a second container
on a private network and is never published on a host port. Native Python
systemd installation remains available for existing deployments and
compatibility.
Connected installs pull versioned runtime images from
`ghcr.io/aydinguven/devcloud` by default; air-gapped installs load the same
images from their verified bundle archives.

After installation, add each workspace image under **Admin > Workspace
Image'ları** from a GHCR/internal-registry reference or an OCI/Docker tar
archive. The controller normalizes and verifies the archive, then enrolled
workers download it over the authenticated controller connection. Create worker
records under **Admin > Worker'lar** and retain each one-time enrollment token.
For a managed template, disabling its active image removes that template from
the user creation menu and blocks direct create API requests; enabling an image
restores it. Resource flavors can be enabled or disabled independently under
**Admin > Çalışma Alanları**. These controls affect new workspaces only, so an
existing workspace remains startable and restartable with its saved template
and flavor.

## All-in-one

All-in-one installs the selected controller runtime and the rootful worker
container. The installer creates one node ID/token pair, seeds
that node in the database, and connects the worker over loopback. This is a
convenience deployment, not a separate runtime architecture.
User-triggered workspace snapshots are supported only in this topology until
distributed registry export has its own explicit authorization workflow.

## Worker

New installations run the worker agent as a system Quadlet container. The
container uses host networking and the rootful host Podman API socket at
`/run/podman/podman.sock`; workspace containers and images therefore remain in
the host's root Podman store. The socket is root-equivalent, is mounted only
into the worker container, and does not require a privileged container. This
mode is intended for dedicated, trusted worker VMs. Existing native/rootless
worker installations retain their saved runtime during repair and update.

The exception is an automatically detected NVIDIA GPU worker. Its one-line
bootstrap validates the existing host driver, NVIDIA Container Toolkit, and
CDI devices before enrollment, then selects the native worker agent so those
host capabilities remain directly observable. AI Factory does not install or
modify any NVIDIA component. A failed GPU preflight exits before consuming the
single-use ticket. CPU workers retain the system Quadlet default.

After enrollment, set user GPU-slot quotas and choose the worker's physical GPU
sharing policy under **Admin > Worker Node'ları**. Zero means automatic: RTX
4090 uses two slots, RTX 5090 uses three, and other physical GPUs use one.
MIG instances always remain exclusive single slots.

When the worker can reach the controller, open **Admin > Worker Node'ları** and
select **Yeni Worker Kurulum Komutu Üret**. Run the generated command as root on
the worker within 10 minutes. The command is single-use. It asks only for the
worker name, creates the node and enrollment token through the controller, then
downloads and verifies the controller's current platform release before invoking
the unified installer. Set `AIFACTORY_WORKER_NAME` to suppress the only prompt.

The reusable `/download/install-worker.sh` endpoint is intentionally disabled;
never place a permanent node token in a URL or shell command. If enrollment is
interrupted after the ticket is consumed, generate a fresh command in Admin.

A JSON answer file can be applied with:

    sudo bash deploy/aifactory-setup.sh --yes install worker \
      --answers /root/worker-answers.json

The token file and generated environment files must remain mode 0600.
Worker installation does not prompt for or preload workspace images. After the
agent connects, it reconciles the enabled controller image catalogue every 30
seconds, verifies archive size and SHA-256, loads missing images into rootful
host Podman through its socket, and reports exact synchronized checksums.
Scheduling waits until a worker reports the currently enabled checksum for the
requested template.

### Workspace AI and the on-prem model gateway

The maintained jupyter-python workspace image includes Jupyter AI 3, notebook
magic commands, Claude Code, and the Claude ACP adapter. This preserves the
agent architecture used by the former JupyterHub deployment: Jupyter AI opens
Claude as the default persona, while Claude Code calls an Anthropic-compatible
on-prem gateway. The maintained VS Code image (`vscode-kilo`) ships Kilo Code,
configured from the same central gateway record plus the Kilo Code settings
described below. Native VS Code
Chat and Copilot are disabled. The `terminal-rocky` image intentionally has no
AI assistant and needs no gateway configuration.

Configure the gateway once under **Admin > Entegrasyonlar > Workspace AI ·
Jupyter + VS Code**. The controller encrypts the shared API key at
rest. Every enrolled
worker fetches the central setting on startup and every 30 seconds, so a newly
installed worker needs no local Jupyter AI credential
provisioning.
The same page manages the shared model catalogue, display names, descriptions,
and the model initially selected for a new Claude session. The default catalogue
contains the former on-prem Qwen model plus the GLM, DeepSeek, Qwen Coder, and
Kimi OpenRouter routes.
Remote workers require an HTTPS controller FQDN because workers receive
decrypted shared settings and per-operation secrets over the agent tunnel. The
all-in-one worker may keep its loopback HTTP connection because it never leaves
the host network namespace.

The following `/etc/aifactory/worker.env` values remain available only as a
rolling-upgrade fallback when the controller has no central Jupyter AI record:

~~~ini
JUPYTER_AI_GATEWAY_URL=http://llm-gateway.internal.example:5003
JUPYTER_AI_MODEL=qwen3.6-35b
JUPYTER_AI_GATEWAY_TOKEN=replace-with-shared-gateway-token
JUPYTER_AI_GATEWAY_MODEL_DISCOVERY=false
JUPYTER_AI_MODEL_CATALOG_JSON=[]
~~~

The gateway URL is the root URL, for example
`http://llm-gateway.internal.example:5003`. Do not append `/v1`: AI Factory and
Claude Code append `/v1/messages` and `/v1/models` as needed, while Kilo Code uses
the same root at `/v1` through its OpenAI-compatible provider. A configured URL
ending in `/v1` therefore causes requests such as `/v1/v1/messages` and usually
returns HTTP 404. The root URL must be reachable from the workspace container
network. Do not use 127.0.0.1 unless the gateway runs inside the same workspace
container. Use routable internal DNS/IP, and install the internal CA in the
maintained workspace image when HTTPS uses a private certificate authority.

Use a restricted LiteLLM virtual key for workspaces, never the LiteLLM master
key. Jupyter AI launches Claude through the ACP adapter rather than calling the
gateway directly like an editor extension. The maintained image intentionally
lets that adapter use its bundled compatible Claude runtime; do not set
`CLAUDE_CODE_EXECUTABLE` unless the external CLI/adapter pair has been tested
together. The separately installed `claude` command remains available in the
workspace terminal.

When using that fallback, restart the worker after changing the file:

~~~bash
sudo systemctl restart aifactory-worker.service
~~~

Only newly created workspace containers receive changed environment values.
Existing containers must be recreated to pick up a changed gateway, token, or
model catalogue. Stop the workspace, run `podman rm -f <container-name>` on its
assigned worker, and start it again from AI Factory. The workspace directory is a
bind mount and is preserved. Do not use the dashboard Delete action for this
operation because Delete removes the persistent workspace storage.

The Admin default is only the initial model; it does not lock a workspace to a
single model. Users can switch among the centrally published routes from
Claude's model picker or with `/model`. Catalogue model IDs are sent to LiteLLM
unchanged, so they must exactly match configured aliases such as
`qwen3.6-35b` or `openrouter/provider/model`. Optional gateway discovery reads
LiteLLM's `/v1/models` endpoint. Claude Code only exposes dynamically discovered
gateway IDs beginning with `claude` or `anthropic`; use the explicit catalogue
for other LiteLLM and OpenRouter aliases.

After saving, select a catalogue entry and run **Seçili Modeli Test Et**. The
controller asks every enabled worker to send a real Anthropic-compatible
request with at most four output tokens using the saved gateway and token. A
successful result confirms worker-to-gateway routing, authentication, and model
alias resolution before a workspace is created. An HTTP 404 usually indicates
an extra `/v1`; a model-not-found response indicates that the catalogue ID does
not match a LiteLLM alias available to the shared key.

The shared gateway API key is forwarded as ANTHROPIC_AUTH_TOKEN to every Jupyter
workspace (and still written into the Cline state of existing workspaces on the
retired Cline images). This means each Jupyter user can inspect and reuse the
key. Use a gateway credential intended for this shared audience, restrict it
at the gateway, and rotate it regularly.

#### Kilo Code

The `vscode-kilo` image ships Kilo Code (`kilocode.kilo-code`). Configure it
under **Admin > Entegrasyonlar > Kilo Code**:

- **Kilo Code modelleri**: optional comma-separated subset of the Workspace AI
  catalogue; empty offers the whole catalogue. The Workspace AI default model
  is Kilo Code's default.
- **Skills URL'leri**: comma-separated URLs Kilo Code discovers skills from,
  for example `http://skills.internal.example:8788/`.
- **Katalog URL**: the catalog root without `/mcp`, for example
  `http://catalog.internal.example:8000`, and an optional **Katalog token'ı**
  (sent as `Authorization: Bearer`). With a URL, `kilo.jsonc` gets two MCP
  servers: `kilo-catalog` (remote, `<URL>/mcp`) and `kilo-install`
  (`bash /opt/kilo/LocalMCP.sh` with `CATALOG_URL`), which writes the chosen
  skills, agents, rules, workflows or `AGENTS.md` into the open project's
  `.kilo` folder. Rules load through `instructions: [".kilo/rules/*.md"]`.
  When `open_catalog` finds no browser opener in the container, it returns
  the catalog page URL to open instead.
- **Kod indeksleme**: embedding model (`bge-m3`), vector size (`1024`) and
  the indexing API key. Embeddings go to the Workspace AI gateway's `/v1`
  endpoint and are stored with LanceDB inside the workspace.

Chat requests use the workspace owner's own AI Factory key, which they
request on the **GenAI** tab (AI Factory issues a separate per-user workspace
key for the same LiteLLM user and team). Kilo Code never falls back to the
shared key, so GenAI and Workspace AI must point at the same LiteLLM. Without
a key the user sees a warning while creating the workspace and an
"anahtar yok" hint in Kilo Code's model picker; indexing still works. Kilo's
own cloud provider is disabled (`disabled_providers: ["kilo"]`).

Every Kilo Code user can read the indexing key and the catalog token inside
their own workspace. Restrict the indexing key in LiteLLM to the embedding
model and give it a budget.

AI Factory rewrites `~/.config/kilo/kilo.jsonc` at each container start. When
the owner's key or the Admin settings changed since the container was
created, stopping and starting the workspace recreates the container with the
new settings; the project directory is a bind mount and is preserved, while
other state in the container (for example extra VS Code extensions or files
outside the project directory) is reset. No manual `podman rm` is needed.

#### Rolling out Kilo Code (3.24.0)

3.24.0 replaces TCMB Code with upstream Kilo Code (see CONTAINERS.md,
"Replacing TCMB Code").

1. Update the controller and then the workers to 3.24.0. Older workers
   cannot write the Kilo Code configuration. Migration 33 keeps the TCMB Code
   card's values (the TCMB Library URL becomes the catalog URL) and moves
   `vscode-tcmb` workspaces to `vscode-kilo`.
2. Import `ghcr.io/aydinguven/devcloud:vscode-kilo-3.24.0` (or the
   `devcloud-workspace-vscode-kilo-*.tar.gz` release archive) and, for GPU
   workers, `ghcr.io/aydinguven/devcloud:vscode-cuda-3.24.0` under
   **Admin > Workspace Image'ları**, and wait for the workers to synchronize
   them. Until then existing workspaces keep their TCMB Code containers.
3. Under **Admin > Entegrasyonlar**, keep Workspace AI enabled, then check
   the **Kilo Code** card: catalog URL (no `/mcp`), catalog token, skills
   URLs, models and indexing. Save it.
4. Create or stop/start a Kilo Code workspace as a user who has requested an
   AI Factory key on the GenAI tab. Trust the project if prompted, open Kilo
   Code in the Activity Bar, send a prompt, and check that the request
   appears under that user's key in LiteLLM. Ask it to list the catalog and
   install an item; it appears under the project's `.kilo` folder.
5. Delete the disabled `vscode-tcmb` image records once no workspace uses
   them.

The release checks Kilo Code activation, its agent CLI start, that the agent
resolves the injected MCP servers, chat-input rendering, that LanceDB's native
binding loads and that `LocalMCP.sh` answers MCP. It does not validate
connectivity to your private gateway, catalog or skills server; test them
after rollout.

#### Kilo Code opens but is not configured

Kilo Code then has no "TCMB code" provider in its model picker. In the
workspace terminal:

~~~bash
ls -la ~/.config/kilo/
~~~

- **No `kilo.jsonc`**: AI Factory sent no configuration. The deployment log
  names the cause; the possible causes are:
  - the **Kilo Code** card under **Admin > Entegrasyonlar** is not set to
    distribute (its badge says "Devre Dışı");
  - Workspace AI is disabled, has no gateway URL, or offers no models;
  - the workspace's worker runs a release older than 3.24.0 (see
    **Admin > Node'lar**). New Kilo Code workspaces are not placed on such
    workers.

  Fix the cause, then stop and start the workspace.
- **`kilo.jsonc` exists**: check the agent sees it with
  `"$(ls -d ~/.local/share/code-server/extensions/kilocode.kilo-code-*/bin/kilo)" models litellm`,
  which lists the configured models, and `... debug config`, which shows the
  resolved MCP servers. The folder must be trusted: in Restricted Mode VS
  Code disables Kilo Code entirely.

In JupyterLab, open the chat panel and select Claude. Notebook magic commands
are also installed:

~~~python
%load_ext jupyter_ai_magic_commands
~~~

The current maintained workspace image is
`ghcr.io/aydinguven/devcloud:jupyter-python-3.6.4`. Import it under
**Admin > Workspace Image'ları** as the source for the `jupyter-python`
template. Enrolled workers then receive the controller-managed archive
automatically. A future platform release publishes a new Jupyter tag only when
its image definition (`containers/jupyter-python`) changes, or when a manual
release forces it with `rebuild_workspaces`.

When the target cannot reach an OCI registry, use the `vscode-kilo` and
`jupyter-python` workspace archives attached to the most recent GitHub Release
that rebuilt that image (releases that did not change a workspace do not carry
its archive). `vscode-cuda` has no release archive because it exceeds GitHub's
2 GiB limit; take it from the workspace image pack
(`deploy/package_workspace_images.py`) or `podman save` it on a connected host:

```bash
sha256sum --check devcloud-workspace-TEMPLATE-vVERSION-SHORT_SHA.tar.gz.sha256
gzip --decompress --keep devcloud-workspace-TEMPLATE-vVERSION-SHORT_SHA.tar.gz
```

Upload the resulting `.tar` under **Admin > Workspace Image'ları**, select the
matching template, enable the new image, and wait for enrolled workers to
synchronize it. Worker hosts do not need access to GitHub or a registry.
The four maintained VS Code images must likewise be rebuilt or republished after
this change so their baked-in Cline extension is present on enrolled workers.

## Updates

Production updates are build-once/deploy-many. Git is the release channel, not
the runtime artifact: every application release builds the controller and
worker OCI images once on a trusted builder, exports them into one signed
platform bundle, and deploys that same bundle without building on the VMs.
Workspace images are not embedded in platform bundles. The release publishes
the maintained Jupyter image separately for controller-managed import only when
its image build context changed; ordinary controller or worker updates reuse the
currently enabled workspace image.

The automated GitHub Actions release process, Rocky 10 runner requirements,
Quay secrets, signing configuration, release assets, and the machine-managed
`stable` channel are documented in [RELEASE.md](RELEASE.md).

On the release builder:

    bash deploy/container/build-controller-image.sh
    bash deploy/container/build-worker-image.sh
    python3 deploy/build_platform_update.py \
      --signing-key GPG_KEY_ID \
      --channel-output /tmp/release-channel/aifactory-update-channel.json \
      --channel-url https://artifacts.example/devcloud/devcloud-platform-update-VERSION-COMMIT.tar.gz

Commit `aifactory-update-channel.json` to the selected `stable` branch (or
another configured branch/tag). Hosts before 3.49.1 read
`devcloud-update-channel.json`, so the release workflow also commits a copy
under that name. Store the multi-gigabyte bundle in a Git
release, an internal artifact server, or beside the channel file for a local
repository. The descriptor pins its filename, size, and SHA-256.

The controller installer asks for the Git repository and branch/tag and stores
them in `/etc/aifactory/controller.env`. They can also be supplied directly:

    sudo bash /opt/aifactory/current/deploy/aifactory-setup.sh --yes update \
      --source-type git \
      --repository https://git.example/devcloud.git \
      --ref stable

The same Git source or an uploaded bundle can be selected under **Admin >
System > Platform Güncelleme**. The controller Git form defaults to
`https://github.com/aydinguven/devcloud.git`, branch `stable`, with
**İmzasız güncellemeye izin ver** selected for the current internal release
workflow. Clear the checkbox to require a signature. The controller process
only writes a request; `aifactory-update.path` invokes the root-owned updater.
A pre-update database/configuration backup is created automatically. Worker
inventory reports the running agent version and OTA state through heartbeat
telemetry.

For an air-gapped or local update:

    sudo bash /opt/aifactory/current/deploy/aifactory-setup.sh --yes update \
      --bundle /root/devcloud-platform-update-VERSION-COMMIT.tar.gz

Official releases contain `release.json` and `release.json.asc` and are
verified by `/etc/aifactory/release-keyring.gpg`. Unsigned updates require an
explicit UI checkbox and warning confirmation, or the CLI `--allow-unsigned`
break-glass option. Only use either bypass for a reviewed source you trust.

For an operator-reviewed development source ZIP:

    sudo bash /opt/aifactory/current/deploy/aifactory-setup.sh --yes update \
      --bundle /root/devcloud-source.zip --allow-unsigned

## Moving from the devcloud names (3.49.1)

Releases before 3.49.1 installed everything under `devcloud` names. The first
update to 3.49.1 moves the host to the `aifactory` names before it installs
the new release. Nothing has to be prepared by hand; update the controller
first, then the workers, the usual way (Admin > System or `update` on the
CLI, and worker OTA from Admin > Worker'lar).

The update:

1. checks the host (see below) and, on the controller, writes a backup to
   `/var/lib/aifactory/backups/pre-migrate-v<old version>-<time>.tar.gz`;
2. stops the devcloud services; on a worker it also removes the workspace
   containers (project files stay; the next **Başlat** recreates each
   container, so packages installed outside the workspace folder are lost);
3. renames the bundled PostgreSQL role and database to `aifactory`;
4. renames `/etc/devcloud`, `/var/lib/devcloud`, `/opt/devcloud` and
   `/srv/devcloud-downloads`, and the `devcloud` service account and group
   (uid and gid stay the same);
5. rewrites `controller.env`, `worker.env` and `postgresql.env`
   (`DEVCLOUD_*` keys become `AIFACTORY_*`), the install state, `/etc/hosts`
   entries and the Nginx configuration (`/etc/nginx/conf.d/aifactory.conf`);
6. replaces the `devcloud-*` systemd units, Quadlets, container network and
   ingress helper with `aifactory-*` ones and starts them.

The old paths stay as symbolic links to the new ones until 3.50.1, because
the older updater that started the update still writes its result there.

The update stops before changing anything when:

- `/etc/aifactory`, `/var/lib/aifactory`, `/opt/aifactory` or
  `/srv/aifactory-downloads` already exists;
- one of the old directories, or a directory below it, is a mount point
  (mount it at the new path first);
- a user named `aifactory` already exists;
- the host is a native (rootless) GPU worker: its Podman storage is tied to
  the `devcloud` account, so uninstall it and register it again.

Each change is recorded in `/var/lib/aifactory-migration/journal.json`. If
any later step fails before the new release is recorded, the update reverses
the recorded changes, starts the devcloud services again and reports the
error; fix the cause and update again. After the release is recorded the move
is kept.

If the update is interrupted (for example a power loss), the 3.49.1 installer
undoes the unfinished move the next time it runs and then starts it again.
Until the old units are removed, a reboot resumes the update by itself.
Otherwise run it again from the CLI with the same source, for example:

    sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
      --bundle /root/devcloud-platform-update-VERSION-COMMIT.tar.gz

If that script is missing, the new release was already staged; use
`/opt/aifactory/current/deploy/aifactory-setup.sh` with the same arguments.

## Back up, restore, and uninstall

    sudo bash /opt/aifactory/current/deploy/aifactory-setup.sh backup \
      --output /backup/aifactory-config-db.tar.gz

    sudo bash /opt/aifactory/current/deploy/aifactory-setup.sh backup \
      --include-workspaces --output /backup/aifactory-full.tar.gz

    sudo bash /opt/aifactory/current/deploy/aifactory-setup.sh restore \
      --bundle /backup/aifactory-config-db.tar.gz

    sudo bash /opt/aifactory/current/deploy/aifactory-setup.sh uninstall

Backups contain checksummed members and are written mode 0600 because they
include secrets. PostgreSQL backups use custom-format pg_dump; SQLite uses its
online backup API. Restore stops services, verifies every member, restores
state, reapplies migrations, and starts services.

Uninstall preserves configuration, releases, database, and workspaces by
default. The purge option permanently removes managed data after a typed
confirmation.

## Managed paths

- /opt/aifactory/current: active release link
- /var/lib/aifactory/releases: immutable releases
- /etc/aifactory/controller.env: controller secrets
- /etc/aifactory/worker.env: worker enrollment secrets
- /var/lib/aifactory/installer/install-state.json: non-secret installer state
- /var/lib/aifactory/update-queue: service-writable, root-consumed update handoff
- /var/lib/aifactory/workspaces: default worker storage
- /srv/aifactory-downloads: controller artifacts
