# Rename technical identifiers from devcloud to aifactory — Requirements

## Context

PR #95 renamed the product to AI Factory in user-facing text. The technical
identifiers still say `devcloud`: host paths, the service user, systemd and
Quadlet units, containers, the Podman network, PostgreSQL role and database,
environment variables, image names, release artifacts, the session cookie,
the migrations table and more. Installed controllers and workers depend on
these names, and the old installed code starts every update, so they cannot
be renamed in one release.

Facts that shape the plan (details and line references in `design.md`):

- Every update is started by the **old** installed code. It verifies the new
  bundle (file name pattern, `release.json.asc` against
  `/etc/devcloud/release-keyring.gpg`, `offline/{role}-images/devcloud-{role}.tar`),
  then delegates the plan to the new release's installer, and after that
  publishes the bundle for workers under the old name pattern.
- The installer finds an existing install only through the hardcoded
  `/var/lib/devcloud/installer/install-state.json`. `InstallConfig.from_dict`
  crashes on unknown keys.
- Workers update separately from the controller. The agent protocol has no
  devcloud names, but the image catalog's `image_ref` values must match the
  tags inside the image archives.
- Rootless Podman (native GPU workers) breaks when its user's home moves
  ("database static dir ... does not match our static dir",
  [containers/podman#24938](https://github.com/containers/podman/issues/24938)).
- The proxy strips the session and share cookies by name before forwarding
  requests to workspace containers. A rename that strips only the new names
  would leak old, still-valid cookies into user containers.

## Scope

In scope: every `devcloud` identifier on hosts, in the database, in browsers,
in images and in release artifacts (full table in `design.md`), plus a
migration for installed hosts.

Out of scope:
- Renaming the Git repository (Aydin does it; `design.md` says when).
- Objects already created in external systems: LiteLLM users/keys created with
  `source: "devcloud"` or a `devcloud-` alias, images already pushed to the
  model container registry or Nexus, customer LDAP DNs. New objects get the
  new names.
- Legacy Git-checkout deployments (`deploy/deploy.sh`, `deploy/update.sh`),
  unless Q6 says they are still used.

## Requirements

### R1 — Target names
1. After the migration, an installed host uses only `aifactory` names:
   `/opt/aifactory`, `/etc/aifactory`, `/var/lib/aifactory`,
   `/srv/aifactory-downloads`, user and group `aifactory`, `aifactory-*`
   units, Quadlets, containers, network and images, PostgreSQL role and
   database `aifactory`, `AIFACTORY_*` environment variables.
2. Fresh installs from the first rename release use the new names directly.
3. Release artifacts, the update channel file, image tags, the session
   cookie and the migrations table use the new names once every host can
   read them (R4).

### R2 — No breakage during the transition
1. A host on any release before the rename can update to the first rename
   release the usual way (UI, OTA or CLI), online or air-gapped.
2. A controller and its workers may run different releases during the
   rollout. Mixed versions keep working: workspace start, image sync, file
   transfer, OTA.
3. Logged-in users stay logged in. Saved MLflow filters are kept.
4. Workspace project data, secrets (`SECRET_KEY`, admin password, worker
   tokens, PostgreSQL password), the database and worker enrollment are kept.
5. Old `devcloud-backup-*.tar.gz` archives can still be restored.
6. No release, including a rollback, forwards a session or share cookie to a
   workspace container.

### R3 — Host migration
1. One installer command migrates one host:
   `aifactory-setup.sh migrate-layout`. It shows the plan and asks for
   confirmation (`--yes` skips the question), like the other installer
   commands.
2. It runs preflight checks before it changes anything and stops with a
   clear message if one fails (list in `design.md`).
3. It takes a backup first, using the existing backup format.
4. It records each step in a journal. If a step fails, it undoes the
   completed steps in reverse order and the host is back on the old layout
   with its services running. If the process dies, running the command again
   offers to resume or undo.
5. It is idempotent: running it on a migrated host reports "already
   migrated" and does nothing.
6. Workers report their layout in the heartbeat. Admin > Worker'lar shows
   which workers still use the old layout.

### R4 — Switch-over release
1. The second rename release refuses to update a host that still uses the
   old layout, and the controller update refuses while any enrolled worker
   reports an older release or the old layout. Both name the hosts to fix.
2. It switches the artifact names, image tags, cookie name and migrations
   table, rewrites the remaining database values, and removes the
   compatibility symlinks once nothing uses them.

### R5 — Documentation
1. INSTALL, AIRGAP, WORKERS, CONTAINERS, SELINUX, RELEASE and README use the
   new names, and INSTALL has a "Migrating from devcloud names" runbook.
2. `DEVELOPMENT.md` records the decision and the rollout status per host.

## Open questions

- Q1. Which installations exist, and how is each one set up (controller
  runtime, database mode, number of CPU container workers and native GPU
  workers, air-gapped or not)? The native GPU worker step needs a test host.
- Q2. Are any of `/opt/devcloud`, `/var/lib/devcloud`,
  `/var/lib/devcloud/workspaces` or `/srv/devcloud-downloads` separate mounts
  (fstab or systemd mount units)? The plan refuses to move mount points and
  asks the admin to remount them first.
- Q3. Is a maintenance window acceptable in which every workspace on a worker
  is stopped while that worker is migrated? Their containers are recreated on
  the next start, so changes outside the bind-mounted project/home directory
  are lost (the same as the existing recreate after a Kilo Code settings
  change).
- Q4. Is a CLI command on each host enough, or should Admin > Worker'lar also
  have a "Migrate" button (needs a new agent command)?
- Q5. Can the Git repository be renamed after release 1 ships and before
  release 2? Release 2 points the update source and GHCR at the new name.
- Q6. Does any host still run from a Git checkout with `deploy/update.sh`?
- Q7. Version numbers: 4.0.0 for release 1 and 4.1.0 for release 2?
