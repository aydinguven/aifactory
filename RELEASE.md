# Automated Platform Releases

AI Factory uses Git as a small release channel, not as storage for runtime
artifacts. The release workflow builds controller and worker images once,
packages those exact images into verified update and offline bundles, publishes
the images to GitHub Container Registry (GHCR), uploads the bundles to a GitHub
Release, and advances the
machine-managed `stable` branch.

## Workflows

- There is no CI workflow: pull requests and merges run no tests on GitHub.
  The release workflow runs the full Python test suite (with a PostgreSQL
  service) in parallel with the builds, and the release is published only if
  it passes. Run `python -m pytest -q` locally before opening a pull request.
- `.github/workflows/release-platform.yml` runs only after a manual dispatch
  from `main` or an exact `vMAJOR.MINOR.PATCH` tag. It uses a GitHub-hosted
  Ubuntu runner and performs the build in a privileged, disposable Rocky Linux
  10 container.

The release workflow deliberately does not run for pull requests.

## Release runner

No self-hosted runner registration is required. GitHub creates a fresh
`ubuntu-24.04` runner for each release job. The workflow starts
`rockylinux/rockylinux:10.2` with Docker's `--privileged` option, installs the Rocky build
dependencies there, and runs Podman with the native `overlay` storage driver
on an image store bind-mounted from the runner disk. If overlay is unavailable,
the build falls back to the much slower `vfs` driver. GitHub CLI and Git
operations that publish the finished artifacts remain on the host.

A `release_scope` job validates the version, decides which workspace images
need a build and creates a draft GitHub Release. The platform build and any
workspace image jobs then run in parallel and upload their assets straight to
that draft, so no multi-GB artifacts are staged between jobs. A final
`publish` job waits for both sides, publishes the draft and advances `stable`.
Inside the platform build, the controller and worker images are built in
parallel, and the registry pushes run alongside the bundle packaging.
The release build does not rerun the test suite; that gate is the CI workflow
on `main` (skipped for Markdown-only changes), so tag a commit only after its
CI run has passed.

The standard GitHub-hosted runner has limited temporary storage. The workflow
requires at least 8 GiB free before starting. If future platform images make
the job exceed the standard runner, configure a larger GitHub-hosted runner or
return to a disposable self-hosted release VM.

The job requires outbound HTTPS access to GitHub and GHCR, Python package
indexes, Rocky repositories, Red Hat UBI registries, and the PostgreSQL image
registry.
The runner and Rocky build container are discarded after the job.

## Repository configuration

Create a GitHub Environment named `release`. Required reviewers are
recommended for production releases. Configure Actions workflow permissions to
allow the repository `GITHUB_TOKEN` to write repository contents and create
releases and packages. GitHub-hosted runners must be enabled for the
repository.

GHCR publication uses the built-in `GITHUB_TOKEN`; no registry secret is
required. New GHCR packages are private by default, so make the `aifactory`
container package public after its first publication so connected hosts can
pull anonymously.

Add these environment or repository secrets only when the optional Quay mirror
is enabled:

- `QUAY_USERNAME`: preferably a repository-scoped robot account;
- `QUAY_PASSWORD`: the corresponding robot token.

Optional release-signing secrets:

- `RELEASE_GPG_PRIVATE_KEY`: ASCII-armored private release key;
- `RELEASE_GPG_KEY_ID`: fingerprint of that key.

The signing key must be usable non-interactively in the release job. During the
current compatibility period, tag-triggered releases are unsigned and manual
workflow runs can opt into signing. Signed runs export the public key as
`aifactory-release-keyring.gpg`, embed it in installation/update archives, and
install it as `/etc/aifactory/release-keyring.gpg` for subsequent updates.

The primary registry destination is `ghcr.io/aydinguven/aifactory`. Formal
version tags also mirror release images to
`quay.io/aaslangoren/devcloud`; manual development releases keep Quay
publication opt-in.

## Publishing

For an ordinary commit-specific release, open **Actions**, select
**Release platform bundles**, choose **Run workflow** on `main`, and leave
`release_tag` empty. The generated tag is:

`platform-vVERSION-SHORT_SHA`

For a formal version release, first update `app.__version__`, merge it to
`main`, and push the exact matching tag:

`vMAJOR.MINOR.PATCH`

The workflow rejects a version tag that does not match `app.__version__`.

The workflow publishes:

- immutable and versioned controller and worker tags to GHCR, on every
  release;
- one controller-managed platform update bundle;
- one complete server offline bundle;
- one complete worker offline bundle;
- SHA-256 sidecars;
- the release channel descriptor;
- the public GPG keyring when signing is enabled;
- only for workspace images whose build context (`containers/TEMPLATE`)
  changed since the last published version release: an immutable,
  smoke-tested image to GHCR as `TEMPLATE-VERSION` and
  `TEMPLATE-VERSION-SHORT_SHA`, plus gzip-compressed Docker archives and
  SHA-256 sidecars for `vscode-kilo` and `jupyter-python`. `vscode-cuda`
  exceeds GitHub's 2 GiB asset limit and is registry-only; its job first
  removes the runner's preinstalled SDKs to make disk room. The `vscode-kilo`
  and `vscode-cuda` builds download the Kilo Code VSIX from Open VSX and check
  its pinned checksum (see CONTAINERS.md, "Updating Kilo Code").

Workspace images have an independent lifecycle and are not embedded in platform
bundles. A release where no workspace build context changed builds and ships
only the controller and worker. To force a rebuild (for example to pick up
base-image security updates), run the workflow manually and set
`rebuild_workspaces` to `all` or to space-separated template names. Formal
version tags mirror release images to Quay automatically. For manual workflow
runs, Quay mirroring remains an explicit opt-in.

Because unchanged workspaces are no longer republished, a workspace's newest
GHCR tag and offline archive belong to the last release that changed it, not
necessarily the latest release. The comparison base is the newest `vX.Y.Z`
tag whose GitHub Release was published, so a tag whose release run failed
never hides a change.

Each offline workspace archive (compressed with `pigz`) is checked against
GitHub's 2 GiB per-asset limit before upload.
After downloading, verify the adjacent `.sha256` file, decompress the
`.tar.gz`, and upload the resulting Docker `.tar` under
**Admin > Workspace Image'ları**.

Each generated archive is verified before publication: the platform bundle is
opened through the real updater code path, and the offline bundles are
verified while staged and integrity-checked once compressed. The `stable`
branch is owned by the workflow and contains only
`aifactory-update-channel.json`. Do not edit that branch manually.

## Updating a controller

After a successful release, a connected controller can resolve the channel
from GitHub:

```bash
bash /opt/aifactory/current/deploy/aifactory-setup.sh \
  --yes update \
  --source-type git \
  --repository https://github.com/aydinguven/aifactory.git \
  --ref stable
```

For temporary unsigned development releases, the installed controller must
explicitly allow unsigned updates and the command must add
`--allow-unsigned`. Production releases should enable signing and install the
published public key as `/etc/aifactory/release-keyring.gpg`.

Unsigned worker OTA is independently gated by the controller environment. Set
`WORKER_OTA_ALLOW_UNSIGNED=true` in `/etc/aifactory/controller.env` and restart
`aifactory-controller` only after accepting checksum-only verification. The
default is `false`. Set it back to `false` when signed releases are enabled.

Hosts installed from an older unsigned release need a one-time trust bootstrap
before their first signed worker OTA. Download
`aifactory-release-keyring.gpg` from the matching GitHub Release, verify its
fingerprint against the release environment's configured key, and install it:

```bash
sudo install -D -o root -g root -m 0644 \
  aifactory-release-keyring.gpg /etc/aifactory/release-keyring.gpg
```

New offline installs and successfully applied platform updates persist the
embedded public key automatically.

On success, the controller republishes the same platform bundle to enrolled
workers. Workers obtain it through authenticated controller endpoints; they do
not need direct GitHub access.

## Failed or repeated runs

The release upload is rerunnable. Existing assets for the same release tag are
replaced. The release stays a draft, and `stable` does not move, until all
builds and verification steps succeed; a failed run leaves the draft behind
for the next run to reuse. If bundle publication succeeds but the channel
update fails, rerun the same workflow after correcting branch permissions.

Each GitHub-hosted run starts clean, so there is no persistent Podman image
store or release workspace to maintain.
