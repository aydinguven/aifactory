# Rename technical identifiers from devcloud to aifactory — Requirements

## Context

PR #95 renamed the product to AI Factory in user-facing text. This spec
renames the technical identifiers too: host paths, the service user, systemd
and Quadlet units, containers, the Podman network, PostgreSQL role and
database, environment variables, image names, release artifacts, the session
cookie and the migrations table.

Installed hosts depend on these names, and the old installed code starts every
update, so the rename takes two releases:

- **3.49.1** ("pre-migration"): the first update moves each host to the new
  names. Release artifacts, workspace image tags, the session cookie and the
  migrations table keep their old names, because hosts on 3.29.x still check
  or use them.
- **3.50.1** ("post-migration"): switches those remaining names once every
  host runs 3.49.1 or later.

Facts that shape the plan (details in `design.md`):

- The old installed code verifies the new bundle first: the file name pattern,
  `release.json.asc` against `/etc/devcloud/release-keyring.gpg`, and
  `offline/{role}-images/devcloud-{role}.tar`. Then it hands the plan to the
  new installer, and afterwards publishes the bundle under its old
  downloads root.
- The installer finds an install only through
  `/var/lib/devcloud/installer/install-state.json`.
- Workers update separately from the controller. The agent protocol carries no
  devcloud names, but the catalog's `image_ref` values must match the tags
  inside the image archives.
- The proxy strips session and share cookies by name before forwarding to
  workspace containers.

## Installations (answers from Aydin, 2026-11-08)

- One controller VM and one worker VM, both Podman (container) installs, with
  access to GitHub and Quay.
- No separate mounts under the old roots.
- Workspaces may be stopped or deleted during the move.
- A CLI step is acceptable; no Admin button is needed. In 3.49.1 the move
  runs inside the normal update, so no extra step is needed at all.
- The Git repository is renamed after 3.49.1 ships and before 3.50.1.
- No host runs from a Git checkout with `deploy/update.sh`.

## Requirements

### R1 — Target names
1. After the move, a host uses only aifactory names:
   - `/opt/aifactory`, `/etc/aifactory`, `/var/lib/aifactory`,
     `/srv/aifactory-downloads`;
   - user and group `aifactory`;
   - `aifactory-*` units, Quadlets, containers, network and platform images;
   - PostgreSQL role and database `aifactory`;
   - `AIFACTORY_*` environment variables.
2. Fresh installs from 3.49.1 use the new names directly.
3. 3.50.1 switches the remaining names: release artifacts, the update channel
   file, workspace image tags, the session cookie and the migrations table.

### R2 — No breakage during the transition
1. A 3.29.x host updates to 3.49.1 the usual way (UI, worker OTA or CLI).
2. A 3.49.1 controller works with 3.29.x workers until they are updated.
3. Logged-in users stay logged in. Saved MLflow filters are kept.
4. Project data, secrets, the database and worker enrollment are kept.
5. Old `devcloud-backup` archives can still be restored.
6. No release, including after a rollback, forwards a session or share cookie
   to a workspace container.

### R3 — Host move inside the 3.49.1 update
1. The update detects a devcloud-named host and moves it before installing
   the new release.
2. It checks the host first and stops with a clear message, before changing
   anything, when the move cannot be done in place.
3. On the controller it writes a backup before the move.
4. Every change is recorded in a journal before it is made. When a later step
   fails before the new release is recorded, the update undoes the recorded
   changes and starts the devcloud services again. After that point the move
   is kept.
5. An interrupted move is undone the next time the 3.49.1 installer runs,
   which then starts it again.
6. Native (rootless) workers are refused: their Podman storage is tied to the
   devcloud account. None exist today.

### R4 — 3.50.1
1. Refuses to update a host still in the devcloud layout. On the controller,
   it also refuses while any enabled worker reports a version below 3.49.1.
2. Switches the names in R1.3, rewrites the remaining `localhost/devcloud-`
   image refs and storage paths in the database, and removes the
   compatibility symlinks once nothing uses them.

### R5 — Documentation
1. INSTALL, AIRGAP, WORKERS, CONTAINERS, SELINUX, RELEASE and README use the
   new names, and INSTALL.md describes the move.
2. `DEVELOPMENT.md` records the decision and the rollout.

## Open questions

None for 3.49.1.
