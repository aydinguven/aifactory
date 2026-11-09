# AI Factory worker setup

Distributed worker behavior, enrollment, networking, scheduling, drain, and
failure semantics are documented in [ARCHITECTURE.md](ARCHITECTURE.md).
Installation and non-interactive answer files are documented in
[INSTALL.md](INSTALL.md).

The short path is:

1. Install the controller with the unified installer.
2. Under **Admin > Worker Node'ları**, generate a one-time worker installation
   command. It is valid for 10 minutes.
3. Run that exact command as root on the Rocky/RHEL 9 or 10 worker and enter only the
   worker name. The controller creates the node and token automatically.
4. Verify aifactory-worker.service is active and the node reports online.
5. Add workspace images under **Admin > Workspace Image'ları**. The worker
   downloads and verifies enabled versions automatically; no image archives are
   copied to the worker installation directory. The page shows download,
   verification, Podman load, ready, and failed status for every worker.

Platform updates are separate from workspace images. A clean controller install
publishes its verified platform release for initial worker setup. Later, after
the controller applies a signed platform bundle, it publishes that bundle to
enrolled workers; workers verify its SHA-256 and apply it through their
root-owned update queue.

The worker inventory displays each OTA phase and its detailed result. Selecting
**Güncelle** first checks the installed and published versions, then asks for
confirmation only when an update is available. A worker that already matches
the published release is reported as current without downloading or queuing the
bundle. Downgrades are blocked. If the root updater fails, the inventory shows
its final message and a bounded tail of the installer output instead of only a
generic error badge.

## Temporary unsigned OTA transition

Unsigned worker OTA is disabled by default. To temporarily enable it, set
`WORKER_OTA_ALLOW_UNSIGNED=true` in `/etc/aifactory/controller.env` and restart
`aifactory-controller`. Release downloads remain authenticated and SHA-256
verified, but the bundle signature is not checked. Restore the value to `false`
after signed releases and trusted keyrings are deployed.

Workers running v3.4.9 or older do not understand this controller approval and
still reject unsigned OTA. Upgrade each such worker once from its host:

```bash
sudo bash /opt/aifactory/current/deploy/aifactory-setup.sh --yes update \
  --source-type git \
  --repository https://github.com/aydinguven/aifactory.git \
  --ref stable \
  --allow-unsigned
```

After that one-time transition, the worker inventory's **Güncelle** action can
apply later unsigned releases while the controller switch remains enabled.
Managed controller updates preserve this opt-in in
`/etc/aifactory/controller.env`.

The only required worker-to-controller firewall flow is outbound TCP 443.
Never expose Podman, workspace ports, or a worker management listener.

## HTTPS identity, private CA, and route fallback

Remote worker enrollment requires the controller HTTPS FQDN. Configure it under
**Admin > Çevrim Dışı İndirmeler > HTTPS & Sertifika Yönetimi** before generating
a one-time command. The generated command keeps that FQDN as the URL, TLS SNI,
HTTP Host header, and certificate verification identity.

For a private PKI, upload the CA-only trust bundle in the same panel. The bundle
may contain root and intermediate CA certificates, but never a leaf certificate
or private key. The Admin-generated command authenticates its first script fetch
with the controller certificate's SPKI pin, installs the verified public CA bundle
as `/etc/aifactory/pki/controller-ca.pem`, and records
`AIFACTORY_AGENT_CA_FILE` for both native and container workers. Repair and update
preserve this path and any configured worker mTLS certificate/key pair.

An optional **Worker fallback IPv4** may be configured next to the public
Controller URL. Enrollment always tries normal FQDN resolution first. Only DNS,
connection, or timeout failures activate the fallback; the script validates the
fallback with the same FQDN TLS identity before adding its managed `/etc/hosts`
route. It never changes the controller URL to an IP and never disables ongoing
certificate validation.

Containerized controllers discover their Podman bridge gateway on every start
and trust forwarded headers only from that exact IP and loopback. This keeps WSS
secret delivery working after clean installs, repairs, updates, or Podman network
recreation without a manually maintained `FORWARDED_ALLOW_IPS=*` setting.

## Workspace AI connectivity and settings rollout

Jupyter AI settings are controller-managed. Every enrolled worker fetches the
shared LiteLLM root URL, restricted virtual key, default model, and exact model
catalogue on startup and every 30 seconds. New workers therefore need no local
Jupyter AI credential file.

Use **Admin > Entegrasyonlar > Workspace AI > Seçili Modeli Test Et** before
starting user workspaces. The controller sends a small real request through
every enabled worker, so the result validates the same worker-to-gateway network
path used by workspace containers. The gateway value must be a root URL such as
`http://llm-gateway:5003`, without `/v1`. Model IDs must exactly match the
LiteLLM aliases allowed for the shared key.

If the gateway certificate is signed by a private CA, the test fails with
`CERTIFICATE_VERIFY_FAILED ... unable to get local issuer certificate`. Paste
the root and intermediate CA certificates (PEM, never a private key) into
**Gateway CA paketi** on the same card. Nothing is installed on the worker
host. Workers from 3.26.0 on trust the bundle in the connectivity test and
install it in every new or recreated Jupyter and VS Code workspace. Inside the
container, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` point
at `/tmp/aifactory-ca/ca-bundle.pem` (the image's system roots plus the
bundle), and `NODE_EXTRA_CA_CERTS` at `/tmp/aifactory-ca/extra-ca.pem`.
Existing workspaces are recreated on their next start after the bundle is
saved or changed. Project files on the mounted work directory are kept, but
anything installed elsewhere in the container is lost. Terminal workspaces
do not receive the bundle.

The `vscode-kilo` image contains Kilo Code (`kilocode.kilo-code`). Its
configuration is not part of the 30-second worker sync: the controller renders
`~/.config/kilo/kilo.jsonc` for each workspace (owner's AI Factory key,
Workspace AI gateway and catalogue, Kilo Code Admin settings) and sends it with
the `container.create` request as `kilo_code_config`; the container writes it
at every start. Workers older than 3.24.0 ignore it. The controller does not
place new Kilo Code workspaces on them, and an existing Kilo Code workspace on
one is left unchanged (with a warning in the controller log) until the worker
is upgraded. A Kilo Code workspace whose config changed also keeps its
container while its image is not yet synchronized to its worker. Workspaces on the retired Cline images still receive Cline's
legacy state and `settings/providers.json` from the shared key.

Settings are injected when a workspace container is created. To roll changed
settings into an existing workspace:

1. Stop the workspace in AI Factory.
2. On its assigned worker, run `podman rm -f <container-name>`.
3. Start the workspace again from AI Factory.

The workspace directory is bind-mounted and survives container recreation.
Do not use the dashboard Delete action as a recreation shortcut; Delete removes
the persistent workspace storage and releases its allocation.

## NVIDIA GPU workers and workspace allocation

Use the same one-line registration command for CPU and GPU workers. The script
detects NVIDIA display/3D PCI hardware automatically. Before it enrolls a GPU
worker, it performs read-only checks for:

- a working nvidia-smi driver interface with at least one physical GPU;
- NVIDIA Container Toolkit through nvidia-ctk;
- at least one nvidia.com/gpu CDI device.

AI Factory deliberately does not install, upgrade, or reconfigure the NVIDIA
driver, Container Toolkit, CDI, or MIG. If a check fails, the script prints the
failed prerequisite and exits before the one-time ticket is consumed. Correct
the host stack and run the same command again.

GPU bootstrap automatically selects the native worker agent so host NVIDIA and
CDI telemetry is available without an extra prompt. CPU bootstrap keeps the
containerized worker default. The Admin worker inventory reports the driver and
toolkit readiness, physical GPU model/count, live memory/utilization data, and
discovered MIG instances.

Users with a nonzero GPU-slot quota can select the `g1.shared` flavor. The
scheduler reserves one exact CDI device and a database-unique slot before
container creation. It never passes `nvidia.com/gpu=all`. Stopped and failed
workspaces retain their slot so restart returns to the same device; deleting
the workspace releases it.

Physical GPU sharing defaults are two workspaces for RTX 4090 and three for RTX
5090. Other physical GPUs default to one. Admins can set one, two, or three
slots per physical GPU in the worker inventory; zero means automatic. Each MIG
CDI slice is always one exclusive slot regardless of the override. On a shared
physical GPU, the flavor's VRAM value is scheduling guidance and does not
enforce a hard memory boundary. Use MIG on DGX/HGX systems when hard isolation
is required.

### CUDA VS Code workspaces

The `vscode-cuda` template (**Kilo Code (CUDA)**) is Kilo Code plus the CUDA
13.4 toolkit, cuDNN 9 and NCCL. To use it:

1. Run host NVIDIA driver 580 or newer on the GPU workers (CUDA 13.x
   requirement) and update the workers to 3.24.0 or newer.
2. Import `ghcr.io/aydinguven/aifactory:vscode-cuda-VERSION` under
   **Admin > Workspace Image'ları**. Only workers whose heartbeat reports an
   NVIDIA stack download it (about 3.5 GB compressed); CPU workers skip it.
3. Users pick the template together with a GPU flavor such as `g1.shared`;
   the dashboard locks the CPU flavors for it and the API refuses them.

The driver libraries and `nvidia-smi` come from the host through CDI, so
`nvidia-smi` and `nvcc --version` both work in the workspace terminal.
Frameworks installed with pip/uv, such as PyTorch, bring their own CUDA
runtime wheels and only rely on the host driver.

## Placement tags and GPU/CPU workers

Workspaces are placed only on workers that match two things.

- **Worker class**, detected from the hardware rather than typed in. A worker
  that reports NVIDIA hardware (healthy or not) is a **GPU** worker; any other
  worker is a **CPU** worker. GPU flavors run only on GPU workers. CPU flavors
  never run on GPU workers, so a cluster whose only worker has a GPU cannot
  start CPU workspaces. Internal CPU jobs such as MLflow image builds may still
  use a GPU worker when no CPU worker is free.
- **Placement tags** (optional), managed under **Admin > Worker'lar >
  Yerleşim Etiketleri**, for example `secure-net`. Assign them to a worker with
  its **Yerleşim** button. When a user selects tags, the workspace goes only to
  a worker that carries all of them.

`GPU` and `CPU` are reserved names. A disabled tag is no longer offered on the
create form, but it stays valid on the workers that carry it. A tag cannot be
deleted while it is assigned to a worker. Changing a worker's tags affects only
new workspaces: existing workspaces stay pinned to their worker.

The IDM/ODM datacenter choice was removed in 3.28.0 (migration 37): every
worker can receive any workspace, subject to its class and tags.

The older free-form key=value worker labels (**Ek etiketler**) are
informational only and do not affect placement.
