# AI Factory - Self-Hosted Cloud Development Environment Platform

AI Factory is a lightweight, high-performance cloud development platform built with **Python (FastAPI)** and **Podman**. It enables teams to create, launch, and manage browser-accessible development environments (VS Code / code-server and JupyterLab) on a Linux VM with persistent storage and resource quotas.

---

## Key Features

- **Podman Container Orchestration**: Rootless container lifecycle management with memory & CPU quotas.
- **Persistent Workspace Storage**: Host bind-mounts maintain project files across container restarts and updates.
- **Integrated Browser IDEs**:
  - **Kilo Code (VS Code)**: Python 3, `pip`, `uv`, Node.js 22 LTS, git, the Python, Jupyter, basedpyright and Ruff extensions, and the Kilo Code AI assistant with LanceDB codebase indexing and an Admin-managed skill/agent/rule catalog (MCP). Chat uses the workspace owner's own AI Factory key.
  - **JupyterLab (Python)**: Data science environment with NumPy, Pandas, Matplotlib, Jupyter AI 3, and a Claude ACP persona for on-prem model gateways.

  The Cline-based VS Code templates (Boş Proje, Python 3.14, React/Node.js, Java 21 LTS) were retired in 3.18.1: existing workspaces keep running, new ones use Kilo Code. TCMB Code (TCMB's Kilo Code build, 3.18–3.23) was replaced by upstream Kilo Code in 3.24.0.
- **Native Linux Terminal Workspace**: A full Rocky Linux 10 shell in the browser, served by `ttyd` over the same authenticated proxy as the IDEs. The session runs inside `tmux`, so a dropped connection reattaches instead of killing running work, and the entire home directory persists across restarts. Workspaces carry no `sudo`; the image ships git, Python 3, a C/C++ toolchain, and the usual shell utilities.
- **Resource Flavors**:
  - `t1.nano`: 0.5 CPU, 512 MB RAM (Light scripts and utilities)
  - `t1.micro`: 1 CPU, 1 GB RAM (Default profile for newly registered users)
  - `t1.small`: 1 CPU, 2 GB RAM (Python, React/Node.js, and standard VS Code development)
  - `t1.medium`: 2 CPU, 4 GB RAM (Java, Jupyter, and medium builds)
  - `t1.large`: 4 CPU, 8 GB RAM (Heavy builds, data analysis, and multi-service projects)
  - `t1.xlarge`: 8 CPU, 16 GB RAM (Compute-intensive builds and large notebooks)
  - `g1.shared`: 4 CPU, 16 GB RAM, one NVIDIA GPU slot with at least 8 GB VRAM

  The former `t1.mini` profile remains internally available for existing workspaces but is no longer offered for new deployments.
- **Modular Authentication**:
  - Internal Database Auth (Argon2 / Bcrypt + JWT + HTTP-only cookies).
  - Runtime-configured LDAPS / Active Directory authentication with encrypted bind credentials.
  - Allowed-user and administrator group mapping, including nested AD groups.
  - Read-only user profiles synchronized from AD: username, full name, email, team, directorate, and parent organization.
  - Admin-controlled, versioned live onboarding with per-topic `Göster` / `Atla` choices and per-user progress.
- **Built-in Reverse Proxy & WebSocket Tunneling**: Access all running workspaces without opening separate firewall ports for every container.
- **Modern Responsive Web UI**: Dashboard with dark theme, real-time container log viewer, and administrative oversight.
- **Resource Usage Dashboard**: Host CPU/RAM/disk utilization, per-user CPU/RAM/disk/GPU allocations, and remaining quota on the workspace dashboard.
- **Self-Service Registration**: New users can sign up from the login screen with a default allowance of 1 CPU core, 1 GB RAM, and 10 GB disk; admins can adjust individual quotas.
- **Per-User Quotas**: Admin-managed CPU, RAM, persistent-disk, and GPU-slot limits with workspace deployment enforcement. GPU quota defaults to zero.
- **Hierarchical Quotas**: **Admin > Kullanıcılar** lists müdürlüks, their teams and the members. Quotas are per member and resolve user override → team → müdürlük → default; a blank field inherits the next level, and each value shows where it comes from.
- **Pause to Reclaim Quota**: CPU and RAM are charged only while a workspace holds compute, so pausing one frees its allowance for another. Disk keeps counting paused workspaces because their data stays on the worker, and GPU slots keep counting because a paused workspace holds its accelerator slot. Resuming re-checks the owner's quota and is refused when it would exceed the limit.
- **Outbound-Only CPU and NVIDIA GPU Workers**: Register workers without inbound management ports, validate an existing NVIDIA driver/Container Toolkit/CDI stack, display physical GPU and MIG inventory, and schedule GPU workspaces onto exact CDI devices.
- **GPU Sharing Policy**: RTX 4090 workers default to two workspace slots per physical GPU and RTX 5090 workers to three; admins can override physical GPUs to one, two, or three slots. Every MIG CDI slice is an exclusive single slot.
- **Managed MLflow Tracking & Registry View**: Administrators define one MLflow URL and TLS policy while every user supplies encrypted personal credentials. AI Factory browses experiments, runs, parameters, metrics, artifacts, registered models, versions, aliases, tags, and run-to-model lineage, then injects the effective MLflow environment into newly created or recreated workspaces.
- **Kubernetes image releases**: The deploy modal's Kubernetes target builds a model version's serving image and publishes it to the deploy registry (Nexus) as `rfs_code/app_name:X.Y.Z`, for the platform team to re-tag and deploy. Admins define the registry and the RFS codes under Admin > Kubernetes. Users pick an RFS code and an app name in the modal, and the first release creates the application bound to that MLflow model. See `CONTAINERS.md`.

---

## Sharing a workspace application

Open a workspace's **Web Uygulaması / Port Önizleme** tab, enter the app port (for example `3333`), and create a share link. Password and lifetime in minutes are optional; blank lifetime means no expiry. Owners and administrators can list, copy, and revoke links from the same panel.

Visitors open `/share/<signed-token>`, enter the password when required, and are redirected to `/proxy/<workspace-id>/port/3333/`. They do not need an AI Factory account. Access covers the entire app on that port, including its HTTP actions and WebSockets, but does not authorize other ports or workspace management. The workspace must stay running and its owner active. Apps must support the existing proxy URL prefix (including asset and WebSocket URLs).

Links and password hashes persist in the database. Revocation and expiry block subsequent requests; active shared WebSockets are checked every two seconds. Responses already delivered cannot be recalled. Visitor access cookies last at most 24 hours, capped by link expiry; reopening a valid link grants access again (with the password, if configured). Five incorrect password attempts temporarily lock the link for one minute.

## Architecture Overview

```
User Browser ── HTTPS/WSS :443 ──> AI Factory Controller
                                      ├── UI / API / LDAP
                                      ├── Scheduler / Database
                                      ├── Workspace Port Proxy
                                      └── MLflow API Connector
                                                ▲
                                                │ outbound WSS :443
                           ┌────────────────────┴────────────────────┐
                           │                                         │
                    CPU Worker 01                         NVIDIA GPU Worker 02
                    ├── Worker Agent                         ├── Worker Agent
                    ├── Podman                               ├── Podman
                    └── Local workspace storage              └── Local workspace storage
```

Worker hosts expose no workspace ports. The controller keeps the public proxy URL,
authenticates each request, resolves the assigned node, and carries HTTP and
WebSocket streams over the worker-initiated tunnel. Every workspace is assigned
to a real worker. All-in-one installs that same worker beside the controller;
there is no controller-local runtime fallback. See [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Quick Start (Development)

### 1. Prerequisites
- Python 3.11+
- Virtualenv (`python -m venv .venv`)

### 2. Setup Virtual Environment & Install Dependencies
```bash
# Create and activate virtual environment
python -m venv .venv

# On Linux / macOS:
source .venv/bin/activate

# On Windows (PowerShell):
.\.venv\Scripts\Activate.ps1

# Install requirements
pip install -r requirements.txt
```

### 3. Run Development Server
```bash
python run.py
```
Visit `http://127.0.0.1:8000` in your browser.

- **Default Admin User**: `admin`
- **Default Admin Password**: `admin123`

### LDAPS / Active Directory

Sign in with the local administrator and open **Yönetim Paneli → Kurumsal
Dizin**. Enter the organization LDAP endpoint, such as
`ldaps.example.com:636`. The form supports testing the bind and user search
before enabling directory login.

- Set a strong, persistent `SECRET_KEY` before saving the bind password. The
  application derives a Fernet key from it and never returns the saved password
  through the API or admin page.
- Keep TLS certificate validation enabled. For an internal CA, install it in the
  operating-system trust store or enter its server-side PEM path.
- `İzinli kullanıcı grubu DN` limits login to one AD group. Leaving it empty
  permits every successfully authenticated directory user.
- `AI Factory yönetici grubu DN` maps members to the AI Factory administrator role.
  Other directory users receive the standard user role.
- Nested group lookup uses Active Directory matching-rule-in-chain and can be
  disabled for non-AD LDAP servers.
- Profile fields are synchronized on successful directory login and are
  read-only in AI Factory. The default AD mappings are `sAMAccountName` for the
  username, `displayName` for full name, `mail` for email, `department` for
  team, and `division` for the parent organization. The optional müdürlük
  mapping is empty by default; set it to the AD attribute that carries the
  intermediate müdürlük value. Administrators can change all attribute
  names in the directory settings form.
- When no müdürlük attribute is set, the müdürlük is derived from the AD
  `manager` chain at login: it is the `department` of the nearest manager (or
  the user themselves) whose `title` is one of the unit-head titles (`MÜDÜR`
  by default, comma-separated). The walk stops at a manager in another
  `division`, so `GENEL MÜDÜR` and above never become a müdürlük. Example:
  an `ARAŞTIRMA VE GELİŞTİRME` member reporting to a `MÜDÜR` in
  `YENİLİKÇİ TEKNOLOJİLER` gets that müdürlük, and the admin team list shows
  the team under it. Existing users pick up the value on their next login.
- **AD'den senkronize et** (Admin > Kullanıcılar) reads every directory person
  in one paged search with the service account and places each AD
  `department` under the müdürlük most of its members' chains reach. A member
  whose own chain is broken (missing or stale `manager`) gets the team's
  müdürlük, at login as well. Teams whose chain reaches a Genel Müdür title
  (`GENEL MÜDÜR` by default) first are listed as reporting directly to the
  Genel Müdürlük; the remaining teams are listed with the reason. The sync
  refreshes the team, directorate, müdürlük, unit head and name of existing
  directory users; it never creates or deactivates accounts.
- The sync also runs automatically about a minute after the controller starts
  and then every `DIRECTORY_SYNC_INTERVAL_HOURS` (default 6, `0` disables), so a
  team gets its müdürlük even when nobody from it, or its müdür, has logged
  in. Managers outside the sync search (another OU, or a filter that only
  returns AI Factory users) are read by DN to complete the chain, and a blank
  `division` on a manager no longer ends it.
- When directory login is enabled, public self-registration is disabled. The
  existing local administrator remains available as an emergency fallback.

---

## Update progress

During a controller update, **Admin > Sistem > Platform Güncelleme** shows a
live progress bar (download bytes, the current installer step such as "Adım
7/11 · Servis tanımları yazılıyor", the controller health check, rollback) and
streams the updater output. The root updater writes
`update-queue/progress.json` and `update-queue/output.log`; the panel polls
them every 2 s and shows "Controller yeniden başlatılıyor" while the
controller restarts. Worker OTA reports the same progress through its
heartbeat, every 3 s while an update runs, and **Admin > Worker'lar** shows a
bar per worker.

Download progress and live output come from the *installed* updater and the
step progress from the *target* release, so the first update after 3.15.0
already shows the step bar once the new controller is up; the full view
(download and live output from the start) appears from the update after that.

## Production Deployment on Linux VM

New managed installations use the root-run interactive lifecycle installer:

    sudo bash deploy/aifactory-setup.sh

It supports controller, all-in-one, and worker roles plus update, repair,
status, backup, restore, and uninstall. See [INSTALL.md](INSTALL.md). The
older role-specific scripts below remain only as migration references for
installations created before the unified installer.

For a secure public hostname, follow [the Cloudflare Tunnel deployment guide](CLOUDFLARE.md).
For Rocky/RHEL enforcing mode, follow [the SELinux deployment guide](SELINUX.md).
For controller plus CPU/GPU worker topology, follow [the worker deployment guide](WORKERS.md).

### 1. Clean install from Git

Use this legacy flow on a pre-unified connected controller. Run it as the Linux account that
should own the AI Factory service; the script requests sudo only for operating
system changes:

```bash
git clone https://git.aydin.cloud/aydin/devcloud.git
cd aifactory
bash deploy/deploy.sh
```

The clean installer installs Python, Podman, Nginx, and SELinux prerequisites;
creates persistent workspace/download/ingress directories; installs Python
dependencies; installs the AI Factory service; and configures Nginx as the port
80 entrypoint. Workspace images have a separate lifecycle: import them later
from an OCI registry or archive under **Admin > Workspace Image'ları**. HTTPS
remains disabled until a valid certificate and key are uploaded from Admin.

Verify the clean installation:

```bash
systemctl status aifactory nginx aifactory-ingress.path --no-pager
curl -I http://127.0.0.1:8000/login
curl -I http://aifactory.example.com/login
```

### 2. Update a managed installation

Managed production VMs do not run `git pull` and do not build images. A trusted
builder builds the controller and worker OCI images once and packages both in a
signed platform release. Git stores only the small checksum-pinned release
channel descriptor.

Apply a configured Git channel or a transferred bundle from the host:

```bash
sudo bash /opt/aifactory/current/deploy/aifactory-setup.sh --yes update \
  --source-type git --repository https://git.example/aifactory.git --ref stable

sudo bash /opt/aifactory/current/deploy/aifactory-setup.sh --yes update \
  --bundle /root/aifactory-platform-update-VERSION-COMMIT.tar.gz
```

The same two choices are available under **Admin > System > Platform
Güncelleme**. Select **Güncellemeyi Kontrol Et** first; the page shows the
installed and published versions before enabling **Güncellemeyi Yükle**.
The current internal release channel is temporarily unsigned.
**İmzasız güncellemeye izin ver** remains an explicit opt-in with a second
warning; signing should be restored after trusted keyrings reach all hosts. During the expected
controller restart, the page reports that it is reconnecting instead of
presenting the proxy's temporary HTTP 502 as an update failure. A root-owned
systemd service creates a pre-update backup, loads immutable OCI archives,
applies migrations, and restarts Quadlet services. Worker inventory shows each
running agent version and its latest OTA state. Workspace images remain
independently managed in the image catalogue. Worker OTA refuses downgrades,
compares installed and published versions before confirmation, skips downloads
when they match, and shows the final updater message or bounded command-output
tail directly below the version badge when an update fails.
Read-only database transactions are returned to the connection pool before
long-lived workspace uploads, HTTP streams, and WebSocket tunnels begin, so
active IDE sessions cannot exhaust controller database connections.

Verify the result:

```bash
systemctl status aifactory-controller aifactory-update.path --no-pager
podman ps
curl -fsS http://127.0.0.1:8000/healthz
```

`/healthz` (process alive) and `/readyz` (database reachable) are the probes
that systemd and Podman use. For per-component health, open the public
**`/status`** page or call `GET /api/health`. It covers the database,
controller storage, workers, background tasks, LDAP/AD, LiteLLM, MLflow, the
model registry, HTTPS/TLS and platform updates. Each component reports `ok`,
`degraded`, `down`, `disabled` or `unknown`. The endpoint answers HTTP 503
when a required component is down, so external monitors can poll it.
Anonymous callers get only statuses and short summaries. Administrators also
see error messages and details (per-worker heartbeat, disk, GPU, Podman and
inventory drift) and can bypass the 15 s cache with `?refresh=1`.

See [RELEASE.md](RELEASE.md) for GitHub Actions release automation and runner
setup, [INSTALL.md](INSTALL.md) for manual release-builder commands, and
[AIRGAP.md](AIRGAP.md) for disconnected updates.

### 3. Manual Linux VM Setup

#### Step 1: Install Podman & Python
```bash
# On Ubuntu / Debian:
sudo apt-get update
sudo apt-get install -y podman python3 python3-pip python3-venv git curl nginx
sudo apt-get install -y pigz  # Optional: accelerates offline bundle compression

# On RHEL / CentOS / Rocky Linux / Fedora:
sudo dnf install -y podman python3 python3-pip git curl nginx policycoreutils-python-utils selinux-policy-targeted
sudo dnf install -y pigz  # Optional; repository availability varies
```

#### Step 2: Configure Workspace Storage Directory
```bash
AIFACTORY_SERVICE_USER="$USER" bash deploy/configure_selinux.sh
```

#### Step 3: Add Workspace Images
```bash
# On a connected build host, only when maintaining local image definitions:
chmod +x containers/build_images.sh
./containers/build_images.sh
```

Import the resulting OCI/Docker archives, or a GHCR/internal-registry image
reference, under **Admin > Workspace Image'ları**. Enrolled workers fetch the
enabled images from the controller; the base installation does not build them.
Formal GitHub Releases include downloadable, checksummed offline archives for
the maintained `vscode-kilo` and `jupyter-python` templates. The GPU-only
`vscode-cuda` template (Kilo Code + CUDA 13, cuDNN, NCCL) is published to the
registries only; see WORKERS.md, "CUDA VS Code workspaces".

### Workspace AI through LiteLLM

The maintained `jupyter-python` template includes Jupyter AI, Claude Code, and
the Claude ACP adapter. The `vscode-kilo` template ships Kilo Code; native
VS Code Chat and Copilot are disabled. Configure gateway access under
**Admin > Entegrasyonlar**:

1. **Workspace AI**: enter the LiteLLM root URL, for example
   `http://llm-gateway:5003`. Do not add `/v1`; Claude Code appends its
   Anthropic paths and Kilo Code uses the same root at `/v1`.
2. Store a restricted LiteLLM virtual key for Jupyter. Never distribute the
   LiteLLM master key. Kilo Code does not use this shared key.
3. Publish the exact LiteLLM aliases in **Kullanıcı Model Kataloğu**, save the
   settings, and run **Seçili Modeli Test Et**. The default model is also Kilo
   Code's default.
4. **Kilo Code**: enable it, optionally limit the catalog models offered in
   Kilo Code, enter the skills URLs and the catalog URL (its root, without
   `/mcp`) with an optional catalog token, and store the indexing key with the
   embedding model (`bge-m3`) and vector size (`1024`). Restrict that LiteLLM
   key to the embedding model and a budget: every Kilo Code user can read it,
   and the catalog token, inside their workspace.

AI Factory writes `~/.config/kilo/kilo.jsonc` into each Kilo Code container
every time it starts. Chat uses the owner's own AI Factory key from the
**GenAI** tab (the per-user workspace key, so usage counts towards that
user, team and müdürlük); there is no shared-key fallback. A user without a
key sees a warning while the workspace is created and an "anahtar yok" hint in
Kilo Code's model picker; after requesting a key they stop and start the
workspace, which recreates the container with the key (project files stay on
the bind mount). Admin changes apply the same way. Indexing uses LanceDB
0.26.2 baked into the image (`KILO_LANCEDB_PATH`). With a catalog URL the
config adds two MCP servers: `kilo-catalog` (remote, `<catalog>/mcp`) and
`kilo-install`, the image's `/opt/kilo/LocalMCP.sh`, which writes the chosen
skills, agents, rules, workflows or `AGENTS.md` into the open project's
`.kilo` folder. Rules load through `instructions: [".kilo/rules/*.md"]`. This requires GenAI and
Workspace AI to point at the same LiteLLM.

**Admin > Skill Kütüphanesi** keeps a library of single-file skills: paste or
upload a `SKILL.md` (YAML frontmatter with `name` and `description`, then the
instructions). Every enabled skill is written into Kilo Code workspaces
(`/home/coder/.aifactory/skills`, listed under `skills` in `kilo.jsonc`) and
Jupyter workspaces (`$CLAUDE_CONFIG_DIR/skills`, Claude Code's user skills,
which the Jupyter AI chat uses). The assistant sees each skill's name and
description and loads the instructions when a request matches; users can also
ask for a skill by name. Changes apply when a workspace is created or stopped
and started (the container is recreated once). The enabled skills together
must fit in about 117 KB compressed, and they need workers on 3.27.0 or later.
Skills are readable by every Kilo Code and Jupyter AI user, so do not put
secrets in them.
Disabling Cline uses the extension manager to remove it from a workspace
container without leaving a broken registration. Recreate affected containers
after changing the toggle; project data remains in its bind mount.

New workers fetch these settings automatically. New workspace containers get
the current settings at creation time. To update an existing workspace, stop
it, remove only its stopped Podman container on the assigned worker with
`podman rm -f <container-name>`, then start it again from AI Factory. Its
bind-mounted workspace data is preserved. Do not use the dashboard **Delete**
action for this operation because Delete removes persistent workspace storage.

Platform and workspace-image releases are independent. A controller or worker
update does not require a new workspace image unless its image definition or
bundled dependencies changed. See [INSTALL.md](INSTALL.md#workspace-ai-and-the-on-prem-model-gateway)
for the full gateway, security, and troubleshooting notes.

### GenAI: personal LiteLLM API keys

The **GenAI** tab lets each user create their own LiteLLM user (named after
their lowercase AI Factory username) and a personal API key. The controller
makes every LiteLLM call itself; browsers never see the admin credential.

1. In LiteLLM, create a dedicated user such as `aifactory` with the
   `proxy_admin` role and generate a key for it.
2. Under **Admin > Entegrasyonlar > GenAI**, enter the LiteLLM root URL (for
   example `http://idmvopenuit1:5003`, without `/ui`) and that key. Save, then
   run **Bağlantıyı Test Et**. The test checks that the key's owner is
   `proxy_admin`.
3. Optional defaults: the user role and the budget/reset period are applied
   when a LiteLLM user is created. The allowed models and the key lifetime are
   applied to each new key. Empty fields mean LiteLLM's defaults apply.

A key is shown once and never stored. AI Factory keeps only its LiteLLM token
hash, so it can delete the old key on **Anahtarı yenile**. Spend is tracked per
LiteLLM user, so usage history survives key rotation. If a LiteLLM user with
the same id already exists, AI Factory adopts it instead of creating a new one.

**Teams.** Set **Varsayılan LiteLLM takımı** (for example `TCMB_Standard_User`).
New users, and adopted users who are in no team yet, join that team. **Takım
önceliği** lists the tier teams with the highest first (`TCMB_Pro_User`,
`TCMB_AI_User`, `TCMB_Standard_User`). Each key is bound to the first team the
user belongs to, so that team's model access and limits apply. To change a
user's tier, add them to the new team in LiteLLM. Their next workspace picks up
the new tier automatically, and the GenAI page asks them to rotate their
personal key.

**Usage statistics.** **GenAI > İstatistikler** shows request and token totals,
a daily chart, user, team (AD department) and müdürlük leaderboards, LiteLLM
team rankings and the model split for 7, 30 or 90 days. Every user also gets a
**Takımım** section with their team's totals, rank and members. Unit heads
(see the unit-head titles in the directory settings) also get a **Müdürlüğüm**
section with müdürlük-wide totals and every team in the müdürlük, including
teams without usage. The viewer's own row is highlighted on each leaderboard.
**Admin > Kullanıcılar** adds 30-day LLM usage to every user card, team and
müdürlük. Spend (USD) is shown to admins only. The data comes from LiteLLM's `/user/daily/activity` and
`/team/daily/activity` and is cached for 60 seconds.

**Workspace AI with the user's own key.** Once a user has created GenAI access,
each new or recreated Jupyter or VS Code workspace gets that user's own
encrypted *workspace key* in place of the shared Workspace AI key. This key is
used for `ANTHROPIC_AUTH_TOKEN` and for the Cline config. Usage is then counted
under the user in LiteLLM. Users without GenAI access, older workers, a GenAI URL
that differs from the Workspace AI gateway URL, or an unreachable LiteLLM all
keep the shared key, so workspace creation never fails because of this. Running
workspaces keep the key they were created with until they are recreated.

#### Step 4: Configure Systemd Service
Copy and edit `deploy/aifactory.service` into `/etc/systemd/system/`:
```bash
sudo cp deploy/aifactory.service /etc/systemd/system/aifactory.service
# Replace {{USER}} and {{PROJECT_DIR}} in the file
sudo systemctl daemon-reload
sudo systemctl enable --now aifactory
```

#### Step 5: Configure the Nginx Ingress

Install the root-owned ingress helper and watcher. This also configures the
initial HTTP port 80 entrypoint; HTTPS can then be enabled from Admin:

```bash
sudo bash deploy/install_ingress.sh "$USER"
```

#### Workspace-Safe Service Reload

Use the project helper after application updates instead of `systemctl restart`:

```bash
bash deploy/restart.sh
```

The helper sends `SIGHUP` to Uvicorn's manager so its workers load the new code
one at a time without stopping the systemd unit or its Podman workspace
supervisors. It bounds the reload/start wait, verifies
`http://127.0.0.1:8000/login`, and prints service logs if the application does
not become healthy. The installed unit also supports `sudo systemctl reload
aifactory` after `deploy/aifactory.service` has been copied into place.

---

## Air-Gapped / Offline Installation

AI Factory supports installation on Rocky Linux and RHEL 9.x/10.x x86_64 VMs
that are isolated from the internet but can reach an internal Satellite or
Foreman/Katello service. New bundles include a small distribution-matched local
DNF repository only for bootstrapping `subscription-manager`. Python, Podman,
`crun`, Nginx, SELinux tooling, and database packages are installed from the
repositories enabled by the administrator's Satellite/Foreman registration.
The base VM still needs DNF, systemd, `sudo`, `tar`, and `sha256sum`.

### Step 1: Prepare the Offline Bundle (On Connected Machine)
Commit the source first, then run the fail-fast packager on an
internet-connected Rocky 10 or RHEL 10 machine matching the target distribution.
Its enabled DNF repositories must be reachable; a RHEL builder must have access
to the entitled RHEL repositories. Install `createrepo_c` on the builder, then
select the target CPython major/minor:

```bash
sudo dnf install -y createrepo_c
git status
python3 deploy/package_offline.py --python-version 3.12
```

This generates an ignored archive and checksum under `dist/`. Upload those two
files to a Git release or artifact repository; do not commit the multi-gigabyte
archive to normal Git history.

Workspace images are deliberately excluded from both controller and worker
base bundles. The worker runtime image is included in both roles; server
bundles also include controller and PostgreSQL runtime images. To transport
the maintained workspace image set separately, run:

```bash
python3 deploy/package_workspace_images.py
```

Transfer and verify that archive independently, extract it, and upload each
archive from its `images/` directory under **Admin > Workspace Image'ları**.
You may instead import the corresponding Quay or internal-registry references.

New bundles use `.tar.gz` archives to keep transfer and storage size practical.
The builder uses multi-core `pigz` when available and falls back to Python's
standard gzip writer otherwise. Existing uncompressed `.tar` downloads remain
supported during migration.

### Step 2: Deploy on the Air-Gapped Linux VM
Transfer both generated files to the target Linux VM via approved media, verify
the outer checksum, extract it, and execute the offline installer:

```bash
sha256sum -c aifactory-offline-*.tar.gz.sha256
tar -xzf aifactory-offline-*.tar.gz
cd aifactory

sudo bash deploy/aifactory-setup.sh
```

On the first run, the installer verifies the bootstrap RPM repository, disables
all other repositories for that one transaction, and installs only
`subscription-manager` from the bundle's `file://` repository. It then stops
so the administrator can register the VM using the normal organization and
activation-key workflow:

```bash
subscription-manager register --org='YOUR_ORG' --activationkey='YOUR_KEY'
dnf repolist
sudo bash deploy/aifactory-setup.sh
```

The second run installs all remaining operating-system prerequisites from the
registered Satellite/Foreman repositories, verifies the full offline manifest,
and continues into the interactive installer.

A connected deployment can also publish separate controller and worker bundles
at `/download/`. Administrators can trigger either verified background rebuild
from the Admin page after enabling the download settings documented in
[AIRGAP.md](AIRGAP.md).

After controller installation, open **Admin > Worker Node'ları** and generate a
10-minute, single-use installation command. Run the generated command as root on
the new worker. The script asks only for the worker name; the controller creates
the node and enrollment token, then serves the checksummed platform release.
On a machine with NVIDIA display/3D hardware, the script first checks the
operator-installed driver, NVIDIA Container Toolkit, and CDI devices. It never
installs or changes that stack, and it stops before consuming the one-time
ticket when a prerequisite is missing or unhealthy.

GPU users select the `g1.shared` flavor like any CPU flavor. AI Factory reserves a
specific worker, device UUID, and slot transactionally, then passes only that
exact CDI device to Podman. Stopped and failed workspaces retain the reservation
until deletion. Physical-GPU slot sharing is cooperative: the 8 GB flavor value
is scheduling guidance, not hard VRAM isolation. Use MIG when hardware-enforced
memory and fault isolation are required.

The initial bootstrap/controller address is `http://10.253.6.189` only until
HTTPS is configured. Remote worker commands are generated only after an HTTPS
FQDN is active. The Offline Downloads card also accepts an optional route-only
fallback IPv4 for workers that cannot resolve or route the FQDN directly.

### In-app HTTPS and certificate upload

A clean connected or offline controller installation configures Nginx on port 80.
In **Admin > Çevrim Dışı İndirmeler > HTTPS & Sertifika Yönetimi**, set the
hostname, upload the PEM certificate chain and its unencrypted PEM private key,
then enable HTTPS. When the organization CA is not already trusted by workers,
also upload the CA-only worker trust bundle. AI Factory verifies the server
certificate validity period, server authentication usage, SAN coverage, and
certificate/key match; every worker trust certificate must be a currently valid
CA with certificate-signing authority. The private key is stored only in the
restricted ingress directory and is never returned by the API.

The generated worker command first uses normal FQDN and PKI validation. For a
private CA, its recovery fetch remains authenticated by the server certificate
SPKI pin shown only in the Admin-generated command, then installs the verified
CA bundle for all later HTTPS/WSS traffic. An optional fallback IPv4 changes
only routing: SNI, Host, and certificate validation continue to use the FQDN.

For the planned deployment, the certificate SAN must contain
aifactory.example.com, DNS must resolve that name to the controller, and clients
and workers must trust the organization CA. Keep **Port 80 HTTP fallback**
enabled while that trust is being rolled out. The fallback deliberately does
not enable HSTS. Disabling fallback changes port 80 to a permanent redirect to
HTTPS.

The generated Nginx configuration preserves the /proxy/... path, WebSocket
upgrade headers, and long-lived workspace sessions. Git-based updates install
the ingress watcher automatically. The preserve-existing command in the update
section is only needed if the host was upgraded using an older updater.

Verify both the application and the workspace proxy after applying a
certificate:

    curl -I http://aifactory.example.com/login
    curl --cacert /path/to/internal-ca.pem -I https://aifactory.example.com/login
    sudo nginx -t
    sudo systemctl status nginx aifactory-ingress.path --no-pager

See [AIRGAP.md](AIRGAP.md) for prerequisites, multi-version bundles, Git release
publishing, SELinux notes, and validation commands.

---

## Directory Structure

```
intelligent-nobel/
├── app/
│   ├── main.py                  # FastAPI entrypoint, lifespan & router mounts
│   ├── config.py                # Pydantic Settings & environment variables
│   ├── database.py              # Async SQLAlchemy engine & session factory
│   ├── models/                  # User and Workspace SQLAlchemy models
│   ├── schemas/                 # Pydantic request/response schemas
│   ├── auth/                    # Internal + runtime-configured LDAP/AD authentication
│   ├── orchestrator/            # Podman CLI manager, Flavors, and Templates
│   ├── proxy/                   # Streaming Reverse Proxy & WebSocket router
│   ├── routes/                  # REST APIs (Auth, Workspaces, Admin) & HTML pages
│   ├── static/                  # CSS and JavaScript frontend logic
│   └── templates/               # Jinja2 HTML templates
├── containers/                  # Controller, worker, and workspace Containerfiles
│   ├── build_images.sh          # Podman batch builder
│   ├── vscode-kilo/             # VS Code + Kilo Code (+ pinned LanceDB runtime, LocalMCP.sh)
│   ├── vscode-cuda/             # vscode-kilo + CUDA toolkit, cuDNN, NCCL (GPU workers)
│   ├── jupyter-python/
│   └── terminal-rocky/
├── deploy/                      # Linux VM deployment scripts & systemd unit
├── tests/                       # Pytest test suite (Auth, Workspaces, Podman, Proxy)
├── requirements.txt
├── run.py                       # Local launcher script
└── README.md
```

---

## API Endpoints

| Method | Endpoint | Description | Auth Required |
|---|---|---|---|
| `POST` | `/api/auth/register` | Register a new user | No |
| `POST` | `/api/auth/login` | Login and receive JWT / session cookie | No |
| `POST` | `/api/auth/logout` | Clear session cookie | Yes |
| `GET` | `/api/auth/me` | Get current user profile | Yes |
| `GET` | `/api/workspaces/templates` | List admin-enabled templates available for new workspaces | No |
| `GET` | `/api/workspaces/flavors` | List admin-enabled selectable resource flavors | No |
| `GET` | `/api/workspaces` | List current user's workspaces | Yes |
| `POST` | `/api/workspaces` | Create & deploy new workspace container | Yes |
| `GET` | `/api/workspaces/usage` | Host usage and current user's quota summary | Yes |
| `GET` | `/api/workspaces/{id}` | Workspace detail | Yes |
| `POST` | `/api/workspaces/{id}/start` | Start container | Yes |
| `POST` | `/api/workspaces/{id}/stop` | Stop container | Yes |
| `DELETE` | `/api/workspaces/{id}` | Delete workspace container | Yes |
| `GET` | `/api/workspaces/{id}/logs` | Fetch container logs | Yes |
| `GET` | `/proxy/{id}/{path}` | Reverse proxy to container IDE | Yes (Owner/Admin) |
| `GET` | `/api/admin/users` | List all users | Yes (Admin) |
| `PUT` | `/api/admin/users/{id}/quota` | Update a user's CPU/RAM/disk/GPU-slot quota | Yes (Admin) |
| `GET` | `/api/admin/workspaces` | List all workspaces | Yes (Admin) |
| `GET` | `/api/admin/stats` | System statistics | Yes (Admin) |
| `GET`, `POST` | `/api/admin/skills` | List the skill library, add a `SKILL.md` | Yes (Admin) |
| `PUT`, `DELETE` | `/api/admin/skills/{id}` | Replace, enable/disable or delete a skill | Yes (Admin) |

---

## Running the Automated Test Suite

Run the full pytest suite with:
```bash
pytest -v
```
There is no CI on pull requests; the release workflow runs the suite before publishing, so run it locally before opening a pull request.


## Workspace lifecycle and transfer limits

CPU/RAM/GPU quota checks and placement reservations share a serialized database
transaction (SQLite `BEGIN IMMEDIATE`, PostgreSQL transaction advisory lock).
Restarts check both worker capacity and the owner's quota before reserving
`STARTING`, because a paused workspace releases its CPU and RAM allowance and
has to be re-admitted. That re-check deliberately skips the disk gate so a full
disk cannot lock a user out of the workspace they need in order to clear space.

`creating`, `starting`, `stopping`, `error`, and `running` all charge CPU and
RAM; `stopped` does not. Node placement and per-user quota share that single
definition (`ACTIVE_ALLOCATION_STATUSES`), so the scheduler and the quota
accountant can never disagree about what a worker is carrying.

Workspace names are unique per owner, not globally. Uniqueness is enforced on a
normalized key (case-insensitive, whitespace-collapsed) computed in Python, so
SQLite and PostgreSQL agree regardless of collation.
Failed or uncertain operations retain capacity until an operator successfully
stops or deletes the workspace. Failed stops preserve status; failed deletion
preserves the controller record and worker tracking. A durable deletion receipt
on the worker makes retries safe after a lost response or worker restart.
Controller startup marks interrupted operations as errors so they can be retried.

The **Maksimum Çalışma Süresi** selector is a maximum runtime measured from each
successful start, even during an active IDE session or background job. It is
disabled by default for new workspaces. Existing `auto_stop_minutes` settings
retain their configured values; set them to zero to disable the limit.

Update controller and workers together for the chunked transfer protocol.
Uploads and downloads use acknowledged 256 KiB chunks. Uploads replace each
file atomically after completion; incomplete uploads leave the previous file intact.
HTTP proxy bodies are spooled on the worker, with bounded memory. HTTP responses,
backups, and workspace WebSockets use independent flow-control windows so slow
consumers do not block heartbeats and other commands. Temporary transfer handles
expire after 120 seconds without activity and are closed on worker disconnect.

Limits configurable on controller and workers (configure matching values):

- `FILE_TRANSFER_MAX_BYTES`: 8 GiB, per file, per upload request, and for
  uncompressed backup input.
- `PROXY_MAX_REQUEST_BYTES`: 512 MiB, per proxied request body.
- `WORKER_MAX_TRANSFERS`: 32 simultaneous file/body transfer handles.
- Response streams: 32 per worker connection, four queued data frames each;
  workspace WebSocket messages are limited to 1 MiB.
- Generated Nginx ingress permits 17 GiB request bodies to accommodate the
  existing 16 GiB workspace-image and 8 GiB platform-update limits plus multipart
  overhead. Application-specific limits still apply. Reapply ingress configuration
  on existing installations to receive this change.

Concurrent admission tests run on SQLite and, in the release workflow, on a
dedicated PostgreSQL service. For local PostgreSQL tests,
set `TEST_POSTGRES_URL` (and `TEST_POSTGRESQL_URL`) to a **disposable test database**;
these tests create and drop application tables in that database.
