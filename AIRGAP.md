# Air-Gapped Deployment

The unified lifecycle entry point for newly produced bundles is
sudo bash deploy/devcloud-setup.sh; it prompts for controller, all-in-one, or
worker role. The older deploy_offline.sh commands later in this document
describe pre-unified bundles and remain for migration compatibility. For an
in-place disconnected update, transfer a signed release or reviewed Git ZIP
and use devcloud-setup update with the bundle path; do not extract over the
active release.

This runbook creates Linux x86_64 server or CPU-worker base bundles
from a specific Git commit. The server bundle supports both Controller and
All-in-one installation, including SQLite, bundled PostgreSQL, and external
PostgreSQL choices. Each base bundle contains the required AI Factory source,
Python wheels, a distribution-matched
`subscription-manager` bootstrap repository, an artifact manifest, and SHA-256
checksums. Both roles contain the immutable rootful worker image; the server
bundle also contains the controller and PostgreSQL 16 images. Workspace images
use a separate optional archive and are
added through the controller after installation. All other operating-system
packages come from an internal Satellite/Foreman service after registration.

Generated bundles are intentionally excluded from normal Git history. Commit
and push the packaging code, then attach the generated archive and checksum to
a Git release (or store them in an approved artifact repository).

## 1. Prepare the target VM before isolation

The bootstrap bundle targets Rocky Linux or RHEL 9.x/10.x on x86_64 (built on
the same distribution and major version as the target; CI publishes EL10
bundles only) and
contains only the DNF dependency closure required to install
`subscription-manager`. Rocky and RHEL use separate bootstrap closures.
The VM must be able to reach the organization's Satellite or Foreman/Katello
service after this bootstrap step.

The minimal target VM must already provide DNF, RPM, systemd, `sudo`, `tar`,
and `sha256sum`. Record its distribution before isolation:

```bash
source /etc/os-release
printf 'ID=%s VERSION_ID=%s ARCH=%s\n' "$ID" "$VERSION_ID" "$(uname -m)"
```

Build on the same distribution family as the target. Rocky 10.0, 10.1, and
10.2 share the `rocky-10-x86_64` profile; RHEL 10.x uses the separate
`rhel-10-x86_64` profile.

## 2. Commit and push the source

The packager refuses tracked, uncommitted changes and packages only files known
to Git. On the connected build machine:

```bash
git status
git add .
git commit -m "harden air-gapped packaging"
git push origin main
```

Do not use `git add -f` on generated wheels, image archives, or `dist/`.

## 3. Build the bundle on a connected machine

The machine needs Rocky Linux 10.x or RHEL 10.x, Git, Python/pip, Podman, DNF's
`download` command, `createrepo_c`, internet access to Python/package/container repositories,
and enough free disk space for staged artifacts and RPMs. Building the optional
workspace-image pack additionally needs space for those images twice. RHEL package builders
must have access to entitled BaseOS/AppStream repositories. Install `pigz` on
the builder to compress with multiple CPU cores; packaging still works with a
slower single-threaded gzip fallback when `pigz` is unavailable.

```bash
sudo dnf install -y createrepo_c
```

The default PostgreSQL source is the public upstream SCL CentOS Stream 10
image. To package the entitled RHEL 10 image or an internal mirror instead, set
DEVCLOUD_POSTGRES_SOURCE_IMAGE before running the builder; authenticate to that
registry first when required.

```bash
git pull --ff-only
python3 deploy/package_offline.py --bundle-role server --python-version 3.12
```

Build the standalone CPU-worker installer separately:

```bash
python3 deploy/package_offline.py \
  --bundle-role worker \
  --python-version 3.12
```

Repeat `--python-version` only if one archive must support several target
runtimes:

```bash
python3 deploy/package_offline.py \
  --python-version 3.11 \
  --python-version 3.12 \
  --python-version 3.13
```

The builder:

1. requires a clean tracked working tree;
2. copies only Git-tracked source into a temporary staging directory;
3. downloads Linux x86_64 binary wheels for the selected CPython version;
4. downloads the Rocky/RHEL `subscription-manager` bootstrap dependency
   closure;
5. creates local DNF repository metadata and checksums it with the RPM payload;
6. builds and exports the rootful worker image for both roles;
7. for the server role, builds the controller image and exports it together
   with PostgreSQL 16 as OCI archives;
8. writes offline/MANIFEST.json with artifact sizes and SHA-256 hashes;
9. verifies the stage and creates these ignored files:

```text
dist/devcloud-offline-v<version>-<YYYYMMDD>-<commit>.tar.gz
dist/devcloud-offline-v<version>-<YYYYMMDD>-<commit>.tar.gz.sha256
dist/devcloud-worker-offline-v<version>-<YYYYMMDD>-<commit>.tar.gz
dist/devcloud-worker-offline-v<version>-<YYYYMMDD>-<commit>.tar.gz.sha256
```

The outer bundle is gzip-compressed because the exported Podman layers benefit
substantially from it. The builder automatically uses multi-core `pigz` when it
is installed, reducing packaging time without changing target-host
compatibility; otherwise it falls back to the standard gzip writer. Previously
published plain `.tar` bundles remain downloadable and the worker bootstrap can
still extract them during the transition.

For an already extracted bundle that needs only installer/RPM-repository fixes,
`deploy/package_offline_patch.py` can build a role-specific delta ZIP from the
original archive. The generated `apply-patch.sh` verifies its payload, preserves
the old RPM directory as a backup, merges repository records into the target's
own manifest, and leaves its wheels and container images in place. This permits
compatible earlier bundle commits while still enforcing bundle format and role.

If the local controller, worker, and PostgreSQL image tags are already
known-good, add --skip-image-build; any role-required missing tag still makes
packaging fail.

Build the optional workspace-image transport independently:

```bash
python3 deploy/package_workspace_images.py
```

It creates `dist/devcloud-workspace-images-v<version>-<date>-<commit>.tar.gz`
and its outer SHA-256 file. This archive is not an installer and is not tied to
a controller or worker role. It contains one normalized Linux/amd64 image
archive per maintained workspace template plus a checksummed manifest.

## 4. Publish the binary artifact

Push the source commit normally. In the Git server UI, create a release/tag
such as `airgap-2026-08-25` at that commit and attach both files from `dist/`.
Release assets keep multi-gigabyte images out of Git history while binding the
bundle to a source commit. Git LFS or an internal artifact repository are also
acceptable if your server has suitable quotas.

### Optional: publish and update bundles from the admin page

A connected native AI Factory server exposes verified bundles at /download/ and lets
an administrator rebuild them from the management page. New installations
enable this feature and prepare its directories automatically. For an existing
installation, enable it once with:

```bash
sudo bash deploy/enable_downloads.sh
```

The helper prepares writable build/publication directories, updates the
project-root `.env`, restores standard SELinux file contexts when available,
and reloads AI Factory. Then use the separate **Controller Paketini Güncelle** and
**Worker Paketini Güncelle** controls under **Yönetim > Çevrim Dışı
İndirmeler**.

The update operation downloads wheels and the Rocky/RHEL RPM dependency closure,
builds the worker image and, for server bundles, controller and PostgreSQL
images, consumes substantial disk space, and requires access to package and
container registries.
It does not rebuild or embed workspace images.
Containerized controllers intentionally disable in-application bundle rebuilds
and source updates. Build releases on a dedicated connected builder and apply
them with the host lifecycle installer. On a truly disconnected native server,
keep published downloads available but set DOWNLOAD_UPDATES_ENABLED=False to
prevent rebuild attempts.

Each background job requires a clean tracked Git working tree. It builds into a
temporary directory, verifies both the internal artifact manifest and outer
SHA-256 checksum, copies the new version into the download directory, and only
then deletes older recognized bundle/checksum pairs of the same role. The latest
controller and worker packages are retained together. Status and the last 120 build
log lines are shared across Uvicorn workers and shown separately on the Admin
page.

The public listing is `https://dev.aydin.cloud/download/`. Keep the entire
hostname behind Cloudflare Access if downloads should be restricted.

When workers can reach the controller, generate a 10-minute, single-use command
under **Admin > Worker Node'ları**. Run that exact command as root on the worker.
Only the worker name is requested; the controller creates the node and token and
serves its locally published, checksummed platform release. This works without
internet access because controller and worker communicate only over the internal
network.

The initial Controller URL is `http://10.253.6.189`. Change it at any time under
**Admin > Çevrim Dışı İndirmeler > Worker kurulumunda kullanılacak Controller URL**.
`DOWNLOAD_PUBLIC_BASE_URL` in `.env` remains the first-run fallback, including
when AI Factory runs behind a reverse proxy. The ticket token is short-lived and
single-use; the permanent enrollment token is returned only to the bootstrap
process and never appears in the command line.

## 5. Verify and install inside the air gap

This section is the clean-install flow. Do not extract a new bundle over a live
project directory: runtime database and local configuration files are
intentionally excluded from generated archives.

Transfer both files using approved media. Keep the target's public repository
access disabled for this test. From the transfer directory:

```bash
sha256sum -c devcloud-offline-*.tar.gz.sha256
tar -xzf devcloud-offline-*.tar.gz
cd devcloud
sudo bash deploy/devcloud-setup.sh
```

The first unified-installer run detects the manifest, verifies
`offline/system-rpms/SHA256SUMS`, disables every configured DNF repository for
the bootstrap transaction, enables only the bundle's `file://` repository,
and installs `subscription-manager`. It then exits so registration credentials
never need to be stored in the bundle.

Register the host using the organization's normal activation-key workflow,
verify its repositories, and rerun setup:

```bash
subscription-manager register --org='YOUR_ORG' --activationkey='YOUR_KEY'
dnf repolist
sudo bash deploy/devcloud-setup.sh
```

The second run installs all other operating-system prerequisites from the
registered Satellite/Foreman repositories. It then verifies every artifact in
the manifest before showing the role and database questions. Python
dependencies are installed from bundled wheels with --no-index. Worker roles
load the verified worker image but start without workspace images. Server roles
also load the controller archive and, when selected, PostgreSQL archive. The
worker Quadlet uses host networking and the rootful `/run/podman/podman.sock`
socket to manage workspace containers in the host's root Podman store. Neither
installation path builds nor pulls an image inside the air gap. External
PostgreSQL still requires access to the operator-provided database endpoint.

After the controller and workers are enrolled, transfer the separately produced
workspace-image archive to an administrator machine or the controller:

```bash
sha256sum -c devcloud-workspace-images-*.tar.gz.sha256
tar -xzf devcloud-workspace-images-*.tar.gz
```

Open **Admin > Workspace Image'ları** and upload each archive in the extracted
`images/` directory. If the air-gapped network has an internal OCI registry,
you may instead import each registry reference from the same page. The
controller normalizes and stores the selected version, then serves it over the
existing authenticated controller connection. Workers verify archive size and
SHA-256 before loading it and report the synchronized checksum. They require no
internet or registry access, and scheduling waits until the requested image is
present on a worker.

For a disconnected in-place update, copy the signed
`devcloud-platform-update-v*.tar.gz` bundle to the controller. It already
contains the prebuilt controller and worker OCI archives plus native-worker
Python wheels, contains no workspace images, and requires no external
package-index access from workers. The updater creates a verified pre-update
backup, stages the release, loads the OCI images, runs migrations, restarts the
Quadlet services, and publishes the same bundle for enrolled workers.

Apply the signed bundle:

```bash
sudo bash /opt/devcloud/current/deploy/devcloud-setup.sh --yes update \
  --bundle /root/devcloud-platform-update-v3.4.7-COMMIT.tar.gz
```

The release is rejected unless its manifest signature chains to
`/etc/devcloud/release-keyring.gpg`. It may alternatively be uploaded from
**Admin > System > Platform Güncelleme**. For an operator-reviewed unsigned
bundle, select **İmzasız güncellemeye izin ver** and accept the additional
warning. Workers only need connectivity to the controller; they download the
published bundle through authenticated endpoints.

## 6. Enable HTTPS from Admin

The controller remains usable over HTTP when no certificate exists. To enable TLS,
open **Admin > Çevrim Dışı İndirmeler > HTTPS & Sertifika Yönetimi**:

1. set the hostname to devcloud.example.com;
2. upload the CA-issued certificate chain in PEM format;
3. upload the matching, unencrypted PEM private key;
4. if workers do not already trust the issuing PKI, upload the CA-only worker
   trust bundle (root and intermediate CA certificates, never a private key);
5. optionally set the worker fallback IPv4 next to the public Controller URL;
6. leave HTTP fallback enabled until DNS and internal CA trust are confirmed;
7. select **Kaydet ve Nginx'e Uygula**.

The panel rejects expired/not-yet-valid certificates, SAN mismatches, non-server
certificates, mismatched private keys, and oversized uploads. Nginx is tested
before reload and the previous active files are restored on failure. The
root-owned devcloud-ingress.path unit handles only fixed files under
/var/lib/devcloud/ingress; the application user is not granted passwordless
sudo.

The application URL used by worker bootstrap changes to
https://devcloud.example.com when HTTPS is enabled. New remote worker commands
require this HTTPS FQDN. The command uses an Admin-delivered SPKI pin to
bootstrap an uploaded private CA bundle, then keeps normal CA and hostname
validation enabled. A configured fallback IPv4 is tried only after FQDN network
failure and is installed as a route for the same FQDN; worker credentials are
never sent to an `https://IP` or HTTP fallback. Before using the one-line worker
installer, ensure the worker can reach either the FQDN route or the configured
fallback IP. No HSTS header is emitted while the port 80 fallback is supported.
