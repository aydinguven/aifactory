# AI Factory deployment architecture

## Supported topology

Distributed deployment is the canonical architecture. It has two roles:

- **Controller**: UI/API, authentication, scheduler, database access, ingress,
  worker control tunnels, release publication, and HTTP/WebSocket proxying.
  It never starts workspace containers or reads workspace files.
- **Worker**: outbound agent, Podman, images, and persistent workspace storage.
  Workers need no inbound management or workspace ports.

An **all-in-one** installation is the same controller and worker services on
one VM. The local worker is enrolled in the database with an ordinary node ID
and token. There is no special local runtime or nullable placement: every
workspace has a worker assignment.

Enterprise controller HA is intentionally outside the current target. The
controller runs one Uvicorn process because live agent tunnels are held in
process. A future HA design must externalize connection ownership and event
routing before adding controller replicas.

The preferred controller packaging is an OCI image supervised by Podman
Quadlet. The optional bundled PostgreSQL database is a separate container on a
private network; Nginx remains host-managed. Workers remain host-native because
they own workspace storage and require root-equivalent Podman control. See
CONTAINERS.md for the packaging boundary and offline image workflow.

## Request and control paths

    Browser -- HTTPS/WSS --> Controller -- worker tunnel --> Worker --> Workspace
                                  |
                                  +--> SQLite or PostgreSQL

Workers initiate the long-lived WSS connection. The controller authenticates
the node token and carries commands, file operations, backups, HTTP streams,
custom ports, and WebSockets over that connection. A worker heartbeat includes
capacity, utilization, capabilities, and its complete managed-container
inventory. The controller records reconciliation drift without deleting
unrecognized worker data.

## Placement and failure behavior

The scheduler considers only enabled, schedulable, connected workers with
sufficient CPU and memory. Reserved resources and live utilization contribute
to the score. Host ports are unique per worker, not globally.

GPU flavors additionally require a healthy allocatable NVIDIA CDI device.
Allocation is a transactionally unique `(worker, device UUID, slot)`
reservation. RTX 4090 and 5090 physical devices automatically expose two and
three cooperative workspace slots respectively, unless an administrator sets
an explicit one-to-three-slot policy. A MIG CDI slice exposes one exclusive
slot. The worker receives only the reserved CDI device, never every GPU.
Stopped and error workspaces keep reservations until deletion.

Physical sharing does not provide hard VRAM or fault isolation: the flavor
memory value is admission metadata. MIG supplies hardware-enforced isolation
and is the intended path for future DGX/HGX B300 deployments.

If no worker is available, workspace creation fails with HTTP 503; it never
falls back to the controller. Existing workspaces remain pinned to their
worker. Deleting a worker is blocked while any workspace remains assigned,
including stopped workspaces. Drain a worker before maintenance. Automated
cross-worker data migration is not in the current target; do not reassign node
IDs without an operator-controlled backup/restore process. Remove assigned
workspaces before deleting a worker.

Node capacity and per-user CPU/RAM quota are charged for the same lifecycle
states: `creating`, `starting`, `stopping`, `error`, and `running`. A `stopped`
workspace releases compute on both axes, so pausing one returns its allowance
to the owner and its headroom to the worker. Resuming is therefore a fresh
admission decision and re-checks worker capacity and owner quota together.
Disk is measured from the filesystem rather than the flavor, so paused
workspaces keep consuming it. GPU slots stay charged while stopped because the
reservation keeps the accelerator slot pinned on the worker; releasing the
quota without releasing the slot would let a user oversubscribe slots that no
one else can claim.

Losing a controller temporarily removes management and proxy access, but
Podman containers continue according to their restart policy. Losing a worker
removes access to the workspaces and local storage on that worker.

## Database choices

- **SQLite**: smallest installation and suitable for all-in-one or a modest
  single-controller deployment. Store it on durable local storage and back it
  up.
- **Bundled PostgreSQL**: installed and managed on the controller VM. This is
  the recommended default when several workers are expected.
- **External PostgreSQL**: operator-managed and supplied as a
  postgresql+asyncpg URL.

The application uses explicit, versioned migrations. Managed installations run
migrations before service startup and disable schema mutation in Uvicorn.
Legacy workspaces without a worker can be migrated automatically only when
there is exactly one unambiguous target worker.

## Releases and reconciliation

Managed releases are immutable version-and-revision directories under
/var/lib/aifactory/releases, selected by /opt/aifactory/current. Updates stage
the new release atomically, verify its
manifest/signature, run migrations, refresh systemd units, and restart the
selected services.

Authenticated administrators may select a Git release channel or upload a
signed platform bundle. Git carries a checksum-pinned channel descriptor; the
runtime artifact contains prebuilt controller and worker OCI archives and
explicitly excludes workspace images. The web process only writes a queue;
`aifactory-update.path` invokes a root-owned one-shot service to verify the
signature, back up controller state, apply the release, and publish it to
workers. Workers obtain releases through node-authenticated endpoints, validate
SHA-256, and use the same root-owned queue. Production web requests cannot
authorize unsigned root updates.

## Images and storage

Workspace images are managed separately from platform releases. The controller
imports them from an OCI registry or tar archive, normalizes and verifies them,
and serves them to workers over authenticated endpoints. Workspace snapshots remain all-in-one
only: publishing a snapshot could export user data and therefore requires a
future, separately authorized registry workflow. Workspace data remains on the
assigned worker unless external storage is mounted at the configured workspace
root.

## Network flows

- Browser to controller: TCP 443, and optionally 80 for redirect/fallback.
- Worker to controller: TCP 443 outbound.
- Controller to PostgreSQL, LDAP, the administrator-configured MLflow server,
  and the image registry. MLflow credentials are encrypted per user and
  resolved only for the authenticated user making tracking or registry
  requests. The controller reads experiments, runs, parameters, metrics,
  artifact metadata, and model-version lineage; write operations remain in MLflow.
  The one exception: after a Kubernetes image release, the controller sets the
  `aifactory.image` and `aifactory.image_digest` tags on that model version with
  the publishing user's credentials.
- Controller to the deploy registry (Nexus) configured in Admin > Kubernetes,
  for Kubernetes image releases. Only the controller pushes there (`skopeo
  copy` from the Model Container Registry); workers never connect to it.
- Worker to image registry and package repositories as configured.
- Controller and workers to the syslog collector (QRadar) configured in
  Admin > Günlükler, over UDP, TCP or TLS. See [LOGGING.md](LOGGING.md).

When both the managed server and a user's MLflow connection are enabled,
AI Factory passes the effective tracking URL and that user's credentials only to
their newly created or recreated workspace container.


Do not expose Podman sockets, worker workspace ports, or worker management
ports.
