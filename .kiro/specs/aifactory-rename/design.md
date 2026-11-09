# Rename technical identifiers from devcloud to aifactory — Design

## Approach

| Release | Ships under | What it does |
|---|---|---|
| **3.49.1** | old artifact names | Uses aifactory names everywhere else. Its update moves a devcloud host to the new names (journaled, with undo). Reads old and new names wherever an older or newer release may have written them. |
| **3.50.1** | new artifact names | Requires every host on 3.49.1 or later. Switches artifact names, workspace image tags, cookie and migrations table. Removes the compatibility symlinks. |
| later | new | Removes the old-name fallbacks. |

**Why the move runs inside the update.** An earlier draft had a separate
`migrate-layout` command. Running the move inside the update has these
advantages:
- the 3.49.1 installer only needs to know the aifactory layout, plus one
  module (`app/installer/legacy_layout.py`) that knows the devcloud names;
- there is no second rollback path, because on a devcloud host the migration
  undo replaces the normal release rollback;
- the controller and its workers move with the normal UI update and worker
  OTA, with no manual step.

**Alternative considered: reinstall and restore.** This needs less code, but
it means manual work and downtime on every host, re-enrolling every worker,
and no undo.

## Update mechanics the plan depends on

**Queued (UI/OTA) update.**
- `devcloud-update.path` starts `devcloud-update.service`, which is a oneshot
  unit.
- That unit runs the old `queued_update.py`, which calls the old
  `deploy/devcloud-setup.sh` from its own tree (`queued_update.py:75`).
- `queued_update.py` resolves `/var/lib/devcloud/update-queue` when it
  starts. It writes `status.json` there and removes `running.json` after the
  child exits.

**Old CLI** (`cli.py`, 3.29.2), in order:
1. Loads state from `/var/lib/devcloud/installer`.
2. Verifies `release.json.asc` with `/etc/devcloud/release-keyring.gpg`.
3. Verifies `offline/MANIFEST.json` and `platform-release.json`; the latter
   requires `offline/{role}-images/devcloud-{role}.tar`.
4. Delegates to the new release: `python -m app.installer update
   --prepared-root <new>`, with `DEVCLOUD_INSTALLER_DELEGATED=1` and
   `DEVCLOUD_UPDATE_PROGRESS_FILE` set.
5. On a controller, publishes the bundle into its old downloads root. The
   name must match `^devcloud-platform-update-v…`.

**Update channel.** Hosts before 3.49.1 read `devcloud-update-channel.json`
at the `stable` ref.

## Names in 3.49.1

**Switched to aifactory:**
- Host roots, service user and group.
- Units, Quadlets, container names and the Podman network.
- The ingress helper and Nginx conf.
- The PostgreSQL role and database, and the SQLite file name.
- `AIFACTORY_*` settings, env-file keys and installer variables, and the
  release marker.
- Worker state files, the `/etc/hosts` marker and the SELinux rule.
- Setup and template file names.
- `containers/aifactory-{controller,worker}`.
- Platform image tags `localhost/aifactory-{controller,worker,postgresql}`.
- Internal workspace env names, `/tmp` and in-image paths, and the skills
  folder.
- Logger names, DOM events and the localStorage key (migrated on read).
- `/_aifactory/status`, with `/_devcloud/status` kept as an alias.
- JSON field names and LiteLLM metadata for new objects.
- Workspace container names for new workspaces.
- The dev DB name.

**Kept until 3.50.1** (older code checks or uses them):

| Name | Why | 3.49.1 reads |
|---|---|---|
| `devcloud-platform-update-*`, bundle root `devcloud-<ver>`, `offline/*/devcloud-{role}.tar`, `devcloud-postgresql-16.tar` | checked by 3.29.x installers | both prefixes (`platform_release.py`, `release_catalog.py`, `package_offline.py`, `install_worker.sh`) |
| `devcloud-offline-*`, `devcloud-worker-offline-*`, `devcloud-workspace-*` | download page and image pack of older controllers | both prefixes |
| `devcloud-update-channel.json` | read by 3.29.x | new name first, then old; CI publishes both |
| `devcloud-release-keyring.gpg` release asset | CI-only name | — |
| workspace image tags `localhost/devcloud-*` | DB rows, templates and archives must agree; old workers cannot alias | worker tags the other-prefix twin (`_adopt_renamed_image`) |
| `devcloud_session`, `devcloud_share_*` (set) | a rollback to 3.29.x must strip what is set | both read, both stripped, both cleared on logout |
| `devcloud_schema_migrations` (written) | a rollback must find its table | either table |
| `ghcr.io/aydinguven/devcloud`, `quay.io/aaslangoren/devcloud`, GitHub URLs | repository renamed later | — |
| `devcloud-mlflow-*` registry tags, `{registry}/devcloud-<template>` | names of images already in registries; GC rebuilds the tag from the name | — |

**Kept permanently:** the digest salts in `kilo_code.py` and
`workspace_provisioning.py` are hash inputs, not names. Objects that already
exist in external systems (LiteLLM, registries, LDAP) also keep their names.

## The move (`app/installer/legacy_layout.py`)

**Detection.** `needs_migration` is true when
`/var/lib/devcloud/installer/install-state.json` exists in a real (not
symlinked) `/var/lib/devcloud`, and no aifactory state exists.

**How the CLI uses it.**
- The CLI builds the new configuration with `migrated_configuration`.
- On `update` it passes a `LegacyMigration` to `build_update_plan`.
- Every other command refuses on a devcloud host and asks for the update
  first.
- Before any command, an unfinished journal is recovered.

**Update plan on a devcloud host:**

```
preflight → migrate-preflight → backup (ctl, devcloud names) → migrate-stop
→ migrate-database → migrate-move → migrate-account → migrate-files
→ migrate-units → release → selinux (wrk) → controller-images → python
→ configuration → services → ingress → migrations → restart → state
→ migrate-commit → cleanup → migrate-finish
```

`on_failure` is `LegacyMigration.undo`.

### Steps

**migrate-preflight.** Nothing is changed yet. It refuses when:
- the worker is a native worker;
- an earlier journal is still present;
- a new root already exists;
- an old root is not a directory, is a mount point, or has a mount below it;
- an old root is on a different filesystem than the new root's parent;
- a user named `aifactory` already exists.

**backup** (controller only). `create_backup(layout=LEGACY)` writes
`/var/lib/devcloud/backups/pre-migrate-v<old>-<ts>.tar.gz`. It moves with the
tree.

**migrate-stop.**
- Starts the journal and records the active and enabled devcloud units.
- Stops or disables the units.
- `devcloud-update.path` stays running, so a reboot resumes the update
  through the old updater.
- On workers, removes the workspace containers named
  `^(devcloud|aifactory)-<uid>-[slug-]<id8>$`. The start path recreates them
  with persistent storage (`workspace_routes.py:615`).

**migrate-database.**
- Bundled container database: checks that the database unit is stopped and
  no database container exists. Then it runs a temporary copy of the sclorg
  image in admin-only mode (`POSTGRESQL_ADMIN_PASSWORD` only, from a 0600 env
  file) without `--rm`.
  - It waits until PID 1 is `postgres`, and stops waiting if the container
    exits.
  - It runs an idempotent `DO` block that renames the database and the role,
    then `ALTER ROLE postgres PASSWORD NULL`.
  - Because of admin-only mode, the start does not depend on which role name
    currently exists. The same helper works for undo.
- Native PostgreSQL: the same SQL through `runuser -u postgres psql`.
- SQLite and external databases are not touched here.

**migrate-move.**
- Renames each old root with `rename(2)`.
- Creates a relative compatibility symlink at the old path, journaled before
  it is created.
- The old updater, which is still running, writes its status and publishes
  the bundle through these symlinks.

**migrate-account.**
- Refuses while the devcloud user still has processes.
- Runs `usermod -l aifactory -d /var/lib/aifactory devcloud`, then `groupmod`.
  The group is checked before the user is renamed.
- Rewrites `/etc/subuid` and `/etc/subgid` lines.

**migrate-files.** Rewrites, journaling the original content first:
- the env files: keys `DEVCLOUD_*` → `AIFACTORY_*`, old roots in the values,
  and the PostgreSQL user and database;
- the release keyring, which the release step later replaces;
- the install state: paths and user;
- the `/etc/hosts` marker;
- the SQLite file and its `-wal`/`-shm` files;
- the worker state files, and the storage paths in the agent registry;
- the `current` link and the `.venv` links;
- `devcloud.conf`, which becomes `aifactory.conf`, followed by `nginx -t`
  and a reload.

**migrate-units.**
- Stops `devcloud-update.path`.
- Removes the devcloud unit files, Quadlets and ingress helper, then runs
  `daemon-reload`.
- `devcloud-update.service` is kept while it runs this update.

**Normal update steps.** These run on the new paths. The engine changes:
- it adopts `localhost/devcloud-postgresql:16` by tagging it;
- it only enables `aifactory-update.path` (`defer_update_watcher`), because
  starting it would replay the old updater's `running.json`;
- `selinux` adds the context rule for the new workspace root before the
  restart.

**migrate-commit** marks the journal `committed`. From then on, undo and
recovery only run `finish`.

**migrate-finish** is best effort and never fails the update:
- removes the `devcloud` network and the old SELinux rule;
- removes the journal;
- when inside the old updater, starts a `systemd-run` handover. The handover
  waits until `devcloud-update.service` is inactive, deletes its unit file and
  starts `aifactory-update.path`. A failed handover is reported.

### Undo

1. Stops the aifactory units, and any devcloud units started at boot.
2. Removes the aifactory unit files, Quadlets and helper, the `aifactory`
   network, and the staged 3.49.1 release.
3. Replays the journal in reverse: restores files, renames back, removes the
   symlinks, renames the account and database back, removes the SELinux rule,
   then enables and starts the recorded units.
4. If a layout action fails, it keeps the journal as `undo-failed`, and the
   next installer run retries. Unit start failures are reported but do not
   keep the journal.

### Restore of old backups

Restore accepts `devcloud-backup/`, `etc-devcloud/`, `devcloud.db` and
`devcloud.pgdump`. It restores into the aifactory paths and rewrites the
restored env files and state with the same functions.

## 3.50.1 (next)

**Preflight**
- The host must be on the aifactory layout.
- The controller update also requires every enabled node to report
  `agent_version` 3.49.1 or later.

**Migration 40**
- Renames `devcloud_schema_migrations`.
- Rewrites `localhost/devcloud-` to `localhost/aifactory-` in
  `workspace_images.image_ref` and `custom_templates.image_tag`. Workers tag
  the twins.
- Rewrites the `/var/lib/devcloud/` prefix in `workspaces.storage_path`.

**Code and CI**
- Switches the new defaults for the workspace image tags, the cookie name and
  the share prefix, plus the bundle names and the channel file. The old
  channel file stays frozen at 3.49.x.
- Points the GHCR default and the update source URL at the renamed
  repository.

**Installer cleanup**
- Removes the compatibility symlinks once nothing points into them.

## Testing

`tests/test_legacy_layout.py` uses a fake host on an all-in-one container
fixture. It covers:
- the full move followed by undo, which must restore the tree byte for byte;
- the refusal before any change;
- the run inside the old updater (unit kept, watcher handed over);
- a committed move that must never be undone.

`tests/test_workspace_shares.py` checks that no session or share cookie under
either name is forwarded.

Manual validation on the two VMs is the 3.49.1 rollout (`tasks.md`).
