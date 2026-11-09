# AI Factory container deployment

## Decision

The controller and optional managed PostgreSQL database are packaged as OCI
images and run as rootful Podman Quadlet services. Nginx remains on the host so
the existing root-owned TLS handoff, firewall integration, and SELinux policy
remain intact.

Workers remain host-native for this phase. A worker owns workspace files and
controls host Podman. Putting that agent in a container would still require the
rootful /run/podman/podman.sock, the host workspace tree, disabled SELinux
separation for the socket, and host image management. That adds another image
without removing host coupling and grants the container full root-equivalent
Podman control. The existing systemd worker is the clearer boundary.

All-in-one is therefore a containerized controller plus the ordinary
host-native worker agent on the same VM. Distributed installations use the
same controller stack and add host-native workers normally.

## Runtime layout

    Host Nginx :80/:443 -> 127.0.0.1:8000 -> controller container
                                                    |
                                                    +-> PostgreSQL container
                                                        on private network

The database is never published to a host port. Controller data, download
artifacts, and ingress requests are bind-mounted from their existing managed
paths. The controller root filesystem is read-only and runs as UID/GID 10001.

Container images use Pull=never. Connected build/publish systems produce OCI
archives, and an air-gapped target verifies and loads those archives before
starting the Quadlet units. No registry is required at installation time.

## Build and export

Build the controller image on a connected Rocky/RHEL-compatible builder:

    bash deploy/container/build-controller-image.sh

The default PostgreSQL source is the upstream SCL PostgreSQL 16 image based on
CentOS Stream 10. Build sites that require the RHEL image or a Satellite mirror
can override AIFACTORY_POSTGRES_SOURCE_IMAGE. Then export both images with
checksums:

    bash deploy/container/export-offline-images.sh

For example, to use the entitled RHEL image:

    podman login registry.redhat.io
    AIFACTORY_POSTGRES_SOURCE_IMAGE=registry.redhat.io/rhel10/postgresql-16:latest \
      bash deploy/container/export-offline-images.sh

The resulting dist/container-images directory can be copied into the air gap
for manual use. The standard server air-gap bundle builder embeds the same two
archives under offline/controller-images, records their sizes and SHA-256
digests in the release manifest, and the interactive installer loads them
automatically.

## Workspace-image lifecycle

Workspace images are not part of the controller or worker base installation.
An administrator imports a maintained template image under **Admin > Workspace
Image'ları** from GHCR, another reachable OCI registry, or an OCI/Docker archive.
The maintained VS Code image is `vscode-kilo`: code-server with Python 3, `uv`,
Node.js 22 and Kilo Code (`kilocode.kilo-code` from Open VSX), with native
Chat/Copilot and extension auto-updates disabled. The image contains no keys or
internal host names; at every container start AI Factory writes
`~/.config/kilo/kilo.jsonc` from the Workspace AI and Kilo Code Admin records,
with the owner's AI Factory key for chat, the shared indexing key for
embeddings, the skills URLs and, when a catalog URL is set, the `kilo-catalog`
(remote) and `kilo-install` (local) MCP servers. `kilo-install` is
`/opt/kilo/LocalMCP.sh` (bash, curl and base64 only), copied from
`containers/vscode-kilo/LocalMCP.sh`; it writes catalog items into the open
project's `.kilo` folder. Codebase indexing loads LanceDB 0.26.2 from
`/opt/kilo/lancedb` (`KILO_LANCEDB_PATH`), installed with `npm ci` from the
committed lockfile in `containers/vscode-kilo/lancedb/`. The image also sets
`KILO_TELEMETRY_LEVEL=off` and disables CLI auto-update, sharing, session
ingest, models.dev fetches and LSP downloads, so nothing reaches the Internet
from a workspace by default.
Both Kilo Code images also bake in the Python tooling listed in
`extensions.txt`: the Python and Python Debugger extensions, Python
Environments, basedpyright as the language server (Pylance is not on Open VSX,
so `python.languageServer` is `None`), Ruff and Jupyter with its companion
extensions. Workspaces cannot reach an extension gallery, so users only have
what the image ships.
The `vscode-cuda` image is `vscode-kilo` plus NVIDIA's CUDA 13.4 toolkit from
the Debian 13 repository: nvcc, the runtime and math libraries, the
command-line debugging/profiling tools (the `cuda:*-devel` set without Nsight
Compute), cuDNN 9 and NCCL, with `/usr/local/cuda/bin` on `PATH` and
`CUDA_HOME` set. It contains no NVIDIA driver: `libcuda`, `libnvidia-ml` and
`nvidia-smi` must match the host kernel module, so the host's NVIDIA Container
Toolkit injects them through the CDI device the scheduler assigns, and the
build fails if a driver library ends up in the image. CUDA 13.x needs host
driver 580 or newer. The template is GPU-only: it is refused on CPU flavors,
its image is synchronized only to workers that report an NVIDIA stack, and it
is placed only on 3.24.0+ workers (older ones would start it without the
Kilo Code config). The image is roughly 7 GB unpacked, so the
release publishes it to the registries only, without a GitHub release archive.
The `terminal-rocky` image carries no IDE and no AI assistant: it is a
digest-pinned Rocky Linux 10 base plus a checksum-locked static `ttyd`, which
serves the shell over HTTP and WebSocket on port 7681. Because its `ide_type` is
`terminal`, the worker leaves the image's own `ENTRYPOINT` in charge instead of
injecting IDE environment or rewriting the container command.

### Updating Kilo Code

The Kilo Code VSIX is platform specific. The image installs the **linux-x64**
build straight from Open VSX and verifies its pinned checksum:

    v=7.8.17
    curl -fsSLO "https://open-vsx.org/api/kilocode/kilo-code/linux-x64/$v/file/kilocode.kilo-code-$v@linux-x64.vsix"
    sha256sum "kilocode.kilo-code-$v@linux-x64.vsix"

Set `KILO_CODE_VERSION` and `KILO_CODE_VSIX_SHA256` in
`containers/vscode-kilo/Containerfile` and, to the same values,
`containers/vscode-cuda/Containerfile` (keep `lancedb/` and `LocalMCP.sh`
identical too; the release smoke test checks both images against the
vscode-kilo version), and release AI Factory as usual. The build refuses to run
while the checksum is empty.

### Updating the bundled extensions

`containers/vscode-kilo/extensions.txt` lists one extension per line:
`<publisher.name> <version> <universal|linux-x64> <sha256>`. The build
downloads each VSIX from Open VSX in that order, checks its checksum and
installs it, then fails if any listed extension is missing. Put dependencies
first (Python before Python Debugger, the Jupyter companions before Jupyter).
Extensions with native code (Python Debugger, Ruff) need the `linux-x64`
build. To pin a new version:

    curl -fsSL "https://open-vsx.org/api/<publisher>/<name>/<version>/file/<publisher>.<name>-<version>.vsix" | sha256sum
    curl -fsSL "https://open-vsx.org/api/<publisher>/<name>/linux-x64/<version>/file/<publisher>.<name>-<version>@linux-x64.vsix" | sha256sum

Keep `containers/vscode-cuda/extensions.txt` identical. A change rebuilds both
images in the next release.

### Replacing TCMB Code (3.24.0)

Releases 3.18–3.23 shipped TCMB Code (TCMB's Kilo Code build, `vscode-tcmb`).
Migration 33 moves its Admin settings to Kilo Code (the TCMB Library URL
becomes the catalog URL), renames `vscode-tcmb` workspaces to `vscode-kilo`,
disables every `vscode-tcmb` and `vscode-cuda` image record (they carry TCMB
Code) and unpins their workspaces. A workspace keeps its old TCMB Code
container until the template's new image has synchronized to its worker; its
next start after that pins the new image and recreates the container from it
(project files are kept). A workspace whose container is gone cannot start
until then. Import `vscode-kilo` and `vscode-cuda` 3.24.0 images
under **Admin > Workspace Image'ları** after updating, then delete the disabled
`vscode-tcmb` records.

### Retired VS Code images

`vscode-empty`, `vscode-python`, `vscode-react` and `vscode-java` (Cline 4.1.17)
were retired in 3.18.1. Their definitions are no longer built, but their
templates still resolve, so existing workspaces keep starting on the images
already on the workers, with the legacy Cline wiring. They are hidden from the
catalog and new workspaces are refused. After rolling out `vscode-kilo`,
disable their entries under **Admin > Workspace Image'ları**; this hides them
everywhere without affecting existing workspaces.
The controller converts registry and Docker inputs to a normalized Linux/amd64
OCI archive, records its size and SHA-256, and stores registry credentials only
in a temporary authentication file for the duration of the import.

Enrolled workers poll the authenticated image catalogue, download missing
archives from the controller, verify their size and SHA-256, and load them into
host Podman. Their heartbeat advertises the exact synchronized checksum and
download/load progress; the Admin image catalogue displays a per-worker status
bar and any synchronization error.
Scheduling selects a worker only after it reports the currently enabled version
of the image requested by the workspace template. No registry access is needed
from worker hosts.

Each workspace is pinned to the exact image it was created from. **Devre Dışı
Bırak** only withdraws an image from new workspaces. A worker keeps every image
that one of its workspaces is pinned to, so pinned workspaces still restart.
Enabling or importing a version disables the template's other versions. The
exception is MLflow model images, which are one per model version. **Sil** is
refused while workspaces are pinned to the image, and the error names them.
For an MLflow model image, Sil is refused while a model deployment uses it.
Otherwise it runs the model-build cleanup, which also removes the image from
the workers and the model registry. If a worker is offline or the registry
refuses the delete, the reply is "pending" with the reason. The cleanup then
keeps retrying in the background.

### Kubernetes image releases

The **Kubernetes** target of the MLflow deploy modal publishes a model version
to the deploy registry (Nexus) instead of running it on AI Factory. The platform
team re-tags the image (`X.Y.Z-RC.N` for test, `X.Y.Z-N` for prod) and deploys
it with Helm; database credentials reach the pod through Vault, External
Secrets and `envFrom`, never through AI Factory or the image.

- Admin > Kubernetes holds the deploy registry (`host:port`, account, password,
  an optional "do not verify TLS" switch that also allows plain HTTP, and an
  optional conversion to Docker v2 schema 2) and the RFS codes. An RFS code
  is a project with a namespace, which defaults to the code. The image name
  is `rfs_code/app_name`.
- Every AD user and admin can publish (`app.kubernetes_publish.can_publish_images`).
  In the deploy modal the user picks an RFS code, types the app name and
  enters `X.Y.Z`. An app name the RFS code does not have yet is created with
  the release and bound to that MLflow model; it must start with the RFS
  code. An existing application only accepts the model it is bound to. Admins
  can disable an application, rebind it to another model, or delete it while
  it has no releases. A version already used by the application, or a tag
  that already exists in the registry, is refused. AI Factory never deletes
  anything in the deploy registry. It checks that the tag is free before the
  build and again right before the copy, but `skopeo copy` itself would replace
  a tag pushed in between: enable **Disable redeploy** on the Nexus hosted
  repository so the registry enforces immutable tags.
- The release reuses a ready build of the model version or builds it on a
  worker as usual. A build made only for a release is **not** imported into the
  worker image catalogue, so workers do not download it. The controller then
  runs `skopeo copy` from the Model Container Registry tag to
  `registry/rfs_code/app_name:X.Y.Z` with separate temporary auth files, and
  records the digest. The intermediate tag is cleaned up once no deployment or
  release needs it; a failed release keeps it for 24 hours so a retry does not
  rebuild.
- If anything fails after the copy began (or the controller restarts), the
  release is recorded as published when its tag is in the registry, and
  failed or requeued when it is not. A pushed tag is therefore never left
  behind a failed release.
- Progress and failures are on the Deployments page (Kubernetes Yayınları);
  a failed release can be retried with the same version.
- When **Bamboo Retag Planı** is enabled in Admin > Kubernetes (Bamboo root
  URL, plan key `AIMLOPS-RETAG`, Bearer token stored encrypted), each
  published release queues that plan, which re-tags the image and opens the
  Jira tasks. The controller sends `POST {url}/rest/api/latest/queue/{plan}`
  (form-urlencoded) with `bamboo.variable.pkg.groupid` (RFS code, upper
  case), `pkg.name` (app name, upper case), `pkg.vers` (`X.Y.Z`) and
  `pkg.img.name` (`rfs_code/app_name:X.Y.Z`, lower case, no registry host).
  The trigger is recorded with the PUBLISHED state and sent by the release
  worker, so a restart does not lose it. A Bamboo failure never fails the
  release: the list shows `Bamboo: başarısız` and **Bamboo'yu Yeniden
  Tetikle**. A request cut off by a controller restart is marked failed
  rather than resent, because sending it twice would open duplicate Jira
  tasks; check Bamboo before retrying it.

For one-file transfer into an air gap, a connected builder can produce a
separate checksummed image pack:

    python3 deploy/package_workspace_images.py

Extract that pack and upload its individual `images/*.tar` archives through the
Admin page. This image pack can be updated on its own without rebuilding or
reinstalling controller and worker bundles.

## Lifecycle rules

- Database migrations run before every controller start and are idempotent.
- Schema 24 scopes workspace names to their owner. Installations that already
  hold duplicate names per user are disambiguated during the upgrade with a
  numeric suffix (`MyWorkspace`, `MyWorkspace (2)`), applied to both the display
  name and the uniqueness key.
- Application startup uses one Uvicorn process because worker tunnels are
  still process-local.
- Container hosts never build during an update. A trusted release builder
  creates one signed platform bundle containing the already-built controller
  and worker OCI archives. The Admin page or host installer queues that bundle
  for the root-owned updater, which reloads the Quadlet definitions.
- A Git repository contains only the small release-channel descriptor. It does
  not replace the OCI build or carry multi-gigabyte image archives in normal
  Git history.
- SQLite and external PostgreSQL remain supported; the PostgreSQL container is
  only installed for the bundled database choice.
- Backups target host bind mounts, so they survive image replacement.
