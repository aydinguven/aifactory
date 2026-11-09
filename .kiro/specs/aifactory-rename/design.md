# Rename technical identifiers from devcloud to aifactory — Design

## Approach

The rename takes two releases. A third, ordinary release later drops the
fallbacks.

| Release | Ships under | What it does |
|---|---|---|
| **1** (4.0.0) | old artifact names | Reads old and new names everywhere. Fresh installs use the new layout. Adds `migrate-layout`. Workers report their layout. |
| (rollout) | | The admin runs `migrate-layout` on the controller, then on each worker. |
| **2** (4.1.0) | new artifact names | Requires every host to be migrated. Switches the contracts that older code depended on: artifact names, image tags, cookie name, migrations table. Rewrites the remaining DB values and removes the compatibility symlinks. |
| later | new | Removes the old-name fallbacks. |

**Why release 1 keeps the old artifact names.** The old installed code
verifies release 1's bundle before it hands over (see "Update mechanics"), so
release 1 has to look like an old release from the outside.

**Why the migration is a separate command, not an update step.** The update
already handles release staging, rollback and migrations. Moving the host
layout inside that flow would mix two rollback paths. One example: if the
update failed after the layout moved, the update rollback would re-render the
previous release's `devcloud-*` units over the new paths. As a separate
command, the migration has its own journal and undo. An update either runs
before it or after it.

**Alternative considered: reinstall and restore.** Back up, uninstall,
install the new layout fresh, restore with path rewriting, then re-enroll
the workers and move their workspace data by hand. That is less code, but
it means manual work and longer downtime on every host, re-enrolling every
worker, and no undo. The in-place migration is mostly directory and account
renames, which are fast and reversible.

## Update mechanics the plan depends on

- **Queued (UI/OTA) update.** `devcloud-update.path` starts
  `devcloud-update.service`. That runs the **old** release's
  `app/installer/queued_update.py`, which calls its own
  `deploy/devcloud-setup.sh` (`queued_update.py:75`).
- **What the old CLI checks, and in which order** (`cli.py:430-490`):
  1. Load state from `DEFAULT_STATE_ROOT=/var/lib/devcloud/installer` (`cli.py:30`).
  2. Verify `release.json.asc` with `/etc/devcloud/release-keyring.gpg` (`release.py:202`).
  3. Verify `offline/MANIFEST.json`.
  4. `load_platform_release`, which requires `offline/{role}-images/devcloud-{role}.tar` (`platform_release.py:63`).
  5. Delegate: `python -m app.installer update --prepared-root <new>` with
     `DEVCLOUD_INSTALLER_DELEGATED=1` (`cli.py:178-205`).
  6. After delegation, `publish_platform_bundle` publishes the bundle. It
     requires `^devcloud-platform-update-v…\.tar\.gz$` (`platform_release.py:17-19`).
- **Update channel.** Old controllers and old CLIs read
  `devcloud-update-channel.json` at the `stable` ref (`update_source.py:22`,
  `routes/admin/system.py:68-96`).
- **Worker OTA.** The old worker agent downloads the bundle, writes
  `/var/lib/devcloud/update-queue/pending.json` and triggers the same queued
  flow on the worker (`worker_agent.py:1394-1521`).
- **Rollback** happens only before DB migrations. It points `current` back to
  the previous release and re-renders units with the previous release's
  templates (`engine.py:1273-1286`).
- **State loading.** `InstallConfig.from_dict` and `InstallationState.load`
  call `cls(**values)`, so an unknown key is fatal (`models.py:139-162`,
  `state.py:27-31`).

## Name map

"Rel" is the release that switches the name. Release 1 already reads both
names for everything switched in release 2.

| Old | New | Rel | Notes |
|---|---|---|---|
| `/opt/devcloud`, `/etc/devcloud`, `/var/lib/devcloud`, `/srv/devcloud-downloads` | `/opt/aifactory`, `/etc/aifactory`, `/var/lib/aifactory`, `/srv/aifactory-downloads` | 1 (migrate) | Moved by directory rename. Compatibility symlinks stay until release 2. |
| user and group `devcloud` (home `/var/lib/devcloud`) | `aifactory` (home `/var/lib/aifactory`) | 1 (migrate) | `usermod -l`/`groupmod -n`. UID and GID stay the same, so file ownership does not change. |
| `devcloud-controller.service`, `devcloud-worker.service`, `devcloud-update.{path,service}`, `devcloud-ingress.{path,service}` | `aifactory-*` | 1 (migrate) | Template files renamed. The engine picks unit names from the host layout. |
| Quadlets `devcloud.network`, `devcloud-{controller,postgresql,worker}.container`; containers with the same names; network `devcloud` | `aifactory*` | 1 (migrate) | The PostgreSQL IP is recomputed on the new network (`engine.py:1791-1828`). |
| `/usr/local/libexec/devcloud-apply-ingress`, `/etc/nginx/conf.d/devcloud.conf` | `aifactory-apply-ingress`, `aifactory.conf` | 1 (migrate) | The old conf is removed in the same step, then `nginx -t`. |
| PostgreSQL role and DB `devcloud` (native, bundled container) | `aifactory` | 1 (migrate) | `ALTER ROLE/DATABASE … RENAME`. Native PostgreSQL uses peer auth, so the role must match the OS user. |
| `database/devcloud.db` (SQLite) | `database/aifactory.db` | 1 (migrate) | Includes the `-wal` and `-shm` files. |
| Env keys `DEVCLOUD_*` in `controller.env`, `worker.env`; process vars `DEVCLOUD_INSTALL_*`, `DEVCLOUD_WORKER_GPU_MODE`, … | `AIFACTORY_*` | 1 (read both); migrate rewrites the files | pydantic `AliasChoices("AIFACTORY_X", "DEVCLOUD_X")`. |
| `install-state.json` paths and `service_user` | new values | 1 (migrate) | No new state keys in release 1. |
| `.devcloud-release-id`, worker `.devcloud-agent-registry.json`, `.devcloud-image-state.json`, `.devcloud-image-cache/` | `.aifactory-*` | 1 (migrate; read both) | |
| `/etc/hosts` marker `# devcloud-controller-fallback` | `# aifactory-controller-fallback` | 1 | The new code strips both. |
| SELinux fcontext for `/var/lib/devcloud/workspaces(/.*)?` | for the new path | 1 (migrate) | The new rule is added **before** `restorecon`, otherwise workspaces would lose `container_file_t`. |
| `deploy/devcloud-setup.sh` | `deploy/aifactory-setup.sh` | 1 | Release 1 keeps a two-line `devcloud-setup.sh` shim that execs the new script. The shim is removed later. |
| `DEVCLOUD_INSTALLER_DELEGATED`, `DEVCLOUD_UPDATE_PROGRESS_FILE` | `AIFACTORY_*` | 1 (set and read both) | Old code sets the old names. |
| Release keyring `/etc/devcloud/release-keyring.gpg`; asset `devcloud-release-keyring.gpg` | `/etc/aifactory/…`; `aifactory-release-keyring.gpg` | path: 1 (migrate); asset: 2 | |
| `devcloud-update-channel.json` | `aifactory-update-channel.json` | 1 publishes both; 2 only new | The old file is frozen at release 1 on `stable`, so old hosts always see release 1 first. |
| `devcloud-platform-update-v*`, `devcloud-offline-*`, `devcloud-worker-offline-*`, `devcloud-workspace-*`, `offline/{role}-images/devcloud-{role}.tar`, bundle root `devcloud-<ver>` | `aifactory-*` | 2 | Release 1 accepts both in `platform_release.py`, `download_routes.py`, `package_offline*.py`, `install_worker.sh`. |
| `localhost/devcloud-{controller,worker,postgresql}` | `localhost/aifactory-*` | 1 (migrate tags the existing images) | Image cleanup handles both prefixes (`engine.py:1251`). |
| `ghcr.io/aydinguven/devcloud:{role}-<v>` | `ghcr.io/aydinguven/aifactory:…` | 2 | Fallback only: bundles carry image archives. Depends on Q5. |
| Workspace images `localhost/devcloud-*:latest` (`templates.py`, `images.py:214`, `catalog.py:298`, `backup_service.py:72`, CI) | `localhost/aifactory-*` | 2 | Release 1 workers tag aliases (see "Release 1"). Release 2 rewrites the `localhost/devcloud-` refs in the DB. |
| Workspace containers `devcloud-<uid>-<slug>-<id8>` | `aifactory-…` | 1 | New workspaces get the new name. Existing ones get it when their container is recreated. Stored names are never rewritten while their container exists. |
| Workspace env `DEVCLOUD_WORKSPACE_ID`, `DEVCLOUD_USER_ID` | `AIFACTORY_*` | 1 (inject both) | User scripts may read them. The old names are dropped later. |
| Other workspace env (`DEVCLOUD_KILO_CONFIG_*`, `DEVCLOUD_SKILLS_TGZ`, `DEVCLOUD_CA_BUNDLE_PEM`, `DEVCLOUD_CLINE_*`, `DEVCLOUD_CLAUDE_SETTINGS_JSON`, `DEVCLOUD_VSCODE_SETTINGS_JSON`), `/tmp/devcloud-*`, `devcloud-jupyter` | `AIFACTORY_*`, `/tmp/aifactory-*` | 1 | Injected and read by the worker's own `podman run` script (`podman_service.py:486-652`), so the rename is atomic per worker. |
| In-image paths (`/opt/devcloud-home-skel.tar`, `devcloud-terminal-entrypoint`, `/etc/profile.d/devcloud-terminal.sh`, `DEVCLOUD_TERMINAL_PORT`, tmux `-s devcloud`, `/opt/devcloud/vscode-extensions.txt`, `WORKDIR /opt/devcloud`) | `aifactory` | 1 | |
| Sentinel `~/.devcloud-home-seeded` (persistent home) | `~/.aifactory-home-seeded` | 1 | The entrypoint checks both. Otherwise it would extract the skeleton over the user's dotfiles. |
| `/home/coder/.devcloud/skills` | `/home/coder/.aifactory/skills` | 1 | The start script removes the old directory. |
| Cookie `devcloud_session`, prefix `devcloud_share_` | `aifactory_session`, `aifactory_share_` | 1 reads and strips both; 2 sets new | Release 1 still sets the old name, so a rollback to older code never sees a cookie it does not strip. |
| `devcloud_schema_migrations` | `aifactory_schema_migrations` | 2 | Release 1 reads either table. Release 2 renames it in a transaction. |
| `localStorage` `devcloud.mlflow.savedFilters.v1` | `aifactory.mlflow.savedFilters.v1` | 1 | Copied on first read. |
| DOM events `devcloud:*`, route `/proxy/{id}/_devcloud/status`, JSON fields `devcloud_user_id`, `devcloud_run_url`, `devcloud_users`, logger names `devcloud.*`, temp prefixes, nginx map var, CI-only names | `aifactory` | 1 | Internal. No listeners for the DOM events. The old status route is redirected for one release. |
| LiteLLM `metadata.source`, key alias prefix | `aifactory` | 1 | New objects only. Code always uses the stored alias (`litellm.py:250`). |
| Admin form default `https://github.com/aydinguven/devcloud.git`; `UPDATE_SOURCE` in `controller.env` | the new repo URL | 2 | Only after the repo rename (Q5). An exact old URL is rewritten. |

**Kept on purpose:**
- **The digest salts** `devcloud-kilo-code-config-v2` and
  `devcloud-workspace-ai-{ca,skills}-v1` (`kilo_code.py:48`,
  `workspace_provisioning.py:170-171`). They are hash inputs, not names.
  Changing them would recreate every AI workspace container once more for no
  benefit.
- **Objects in external systems** (see `requirements.md`).

## Shared naming module

`app/naming.py` keeps both name sets in one place, so no other file
hardcodes a prefix.

- `LEGACY` and `CURRENT` are frozen dataclasses. Each holds the roots, the
  service user, the unit, Quadlet, container and network names, the image
  prefix, the env prefix, the marker and state file names, and the
  PostgreSQL role and DB.
- `detect_layout(host_root)` returns `CURRENT` when
  `/var/lib/aifactory/installer/install-state.json` exists as a real
  (non-symlink) path, `LEGACY` when only the old one exists, and `None` when
  neither exists.
- `env(name, default=None)` reads `AIFACTORY_<name>`, then `DEVCLOUD_<name>`.
- `alias_path(path)` maps a path under an old root to the new root and back.
  The worker uses it for every controller-supplied path. It returns the
  first variant that exists.

## Release 1 changes

### Installer
- **Layout-aware.** The engine takes a layout and resolves every path, unit,
  Quadlet, image and env-key name through it. Today they are literals across
  `engine.py`, `cli.py`, `backup.py`, `progress.py`, `queued_update.py`,
  `deploy/*.sh` and the templates.
  - Templates get `{{ETC_DIR}}`, `{{STATE_DIR}}`, `{{QUEUE_DIR}}`,
    `{{UNIT_PREFIX}}` and similar placeholders.
  - Old installers copy unknown placeholders verbatim. That is harmless,
    because each engine renders only its own release's templates.
- **Discovery.** `cli.DEFAULT_STATE_ROOT` probes the new path first, then
  the old one.
- **Fresh installs** use `CURRENT`. Updates keep the host's detected layout.
- **Path guard.** `_validate_config` accepts a path component containing
  either name (`engine.py:676-686`).
- **State loading.** `from_dict` and `InstallationState.load` ignore unknown
  keys and log them. This is only for robustness: release 1 adds no state
  keys.
- **Env files.** The installer writes the layout's key names. The
  re-read logic for existing secrets accepts both
  (`engine.py:1407-1417, 1522-1558`).
- **Explicit paths in `controller.env`.** `UPDATE_QUEUE_ROOT`,
  `INGRESS_STAGING_ROOT`, `INGRESS_APPLY_COMMAND`, `WORKSPACE_IMAGES_ROOT`
  and `DOWNLOAD_BUILD_ROOT` are now always written, so the `config.py`
  defaults never decide the layout.
- **Reading the next release's artifacts.** The verification and publish
  code accepts both names:
  - bundle file pattern, archive paths in `platform-release.json`
  - bundle root, keyring path
  - `DEVCLOUD_/AIFACTORY_INSTALLER_DELEGATED`, the progress-file env var
  - the release marker, image cleanup prefixes
- **Publishing to workers.** Release 1 still publishes the bundle under the
  old name, because workers may still run older code.
- **Backup.** Writes `aifactory-backup/`, `etc-aifactory/` and
  `aifactory.{db,pgdump}` on new-layout hosts.
- **Restore.**
  - It accepts both layouts.
  - It maps `etc-devcloud` to the host's etc dir.
  - It rewrites the old roots, the service user, the PostgreSQL host, role
    and DB names, and the SQLite file name in the restored state and env
    files to the host's layout.
- **`ingress` helper.** `apply_ingress.py` gets its paths from the unit
  file instead of constants.
- **`install_worker.sh` and `deploy_worker_offline.sh`.**
  - They accept both bundle root names, both setup script names and both
    operator env names.
  - The refusal check looks for either state file.

### Controller app
- **Settings.** Fields renamed, with `AliasChoices` for the old env names.
  `COOKIE_NAME` keeps the old default in release 1.
- **Cookies.**
  - The session is read from `COOKIE_NAME`, falling back to the other name.
  - Share cookies are read under either prefix.
  - The proxy strips both names and both prefixes (`proxy/router.py:247-258, 543`).
- **Migrations table.**
  - `migrations.py` picks the table through one helper: the new name if
    it exists, else the old one.
  - `_applied_versions`, `_record_version`, `current_version` and
    `health.py:259` all use that helper.
  - Release 1 still creates the old name on a fresh DB.
- **Workspace container names.**
  - New workspaces get `aifactory-<uid>-<slug>-<id8>`
    (`workspace_provisioning.py:650`).
  - When the start path recreates a missing container whose stored name
    starts with `devcloud-`, it first assigns a new name
    (`workspace_routes.py:615`).
- **Worker layout.** The controller stores the layout each worker reports in
  its heartbeat next to the node's `agent_version` (`agent_routes.py:38-60`).
  Admin > Worker'lar shows it.
- **JS/HTML.** localStorage key migration and DOM event names. The
  `/_devcloud/status` route redirects to `/_aifactory/status`.

### Worker agent
- **Env.** Reads all `AIFACTORY_*`/`DEVCLOUD_*` env through `naming.env`
  (`worker_agent.py:105-136, 286-306, 453-454, 1376-1402, 1577`).
- **State files.** Reads the new names first, then the old ones
  (`worker_agent.py:155-157, 324`).
- **Image alias.** Before downloading `localhost/aifactory-X`, if only
  `localhost/devcloud-X` exists, run `podman tag`. After loading an archive,
  if the expected ref is missing but its other-prefix twin exists, tag it.
  This replaces the "Loaded archive did not provide expected image" failure
  (`worker_agent.py:377-419`).
  - Without this, release 2's DB rewrite would make every worker download
    every image again and then fail.
- **Paths.** Controller-supplied storage paths go through `alias_path`
  (size, backup, delete, file operations).
- **Heartbeat.** Reports `layout`.
- **`podman run`.** Renames the internal env names. Injects both
  `AIFACTORY_` and `DEVCLOUD_WORKSPACE_ID`/`USER_ID`.

### CI (`release-platform.yml`)
- Publishes `aifactory-update-channel.json` next to the old file on `stable`
  and as a release asset.
- Keeps the other artifact names unchanged.

## `migrate-layout`

The command runs as root from the installed tree:
`sudo bash /opt/devcloud/current/deploy/aifactory-setup.sh migrate-layout`.
It is the same flow for every role. Steps marked *(ctl)* or *(wrk)* run only
on that role. All-in-one hosts run both.

### Preflight (nothing changes yet)
1. The installed release is release 1 or later, and the detected layout is
   `LEGACY`.
2. None of the new roots exists. An empty directory is allowed.
3. Each old root is on the same filesystem as the new root's parent
   (`st_dev`). No old root is a mount point. No mount point is below an old
   root. If one is, the command prints the fstab or mount-unit change to
   make and stops (Q2).
4. *(wrk)* No workspace container is running. Workspace containers are found
   with `podman ps -a` names matching `^devcloud-\d+-.+-[0-9a-f]{8}$`.
   The command lists the running ones and stops. The admin can stop them
   from Admin > Workspaces (Q3).
5. No journal exists, or the journal is complete.
6. The PostgreSQL role and DB `aifactory` do not exist yet *(ctl, bundled or native)*.

### Steps
Each step has an idempotent `apply` and `undo`. The journal is
`/var/lib/aifactory-migration.json`, a fixed path outside both trees,
written atomically with mode 0600. Each step is recorded before and after it
runs.

1. **Backup.** Configuration, state and database *(ctl)*, without
   workspaces, to `/var/lib/devcloud/backups/pre-migrate-<ts>.tar.gz`.
2. **Stop.** Stop and disable the legacy units:
   - controller, worker, ingress path, PostgreSQL quadlet
   - `devcloud-update.path`, but not `devcloud-update.service` if it is the
     running unit
   - *(wrk)* `podman rm` the stopped workspace containers. They are
     recreated on the next start with persistent storage
     (`workspace_routes.py:615-640`).
   - Then end the service user's leftover processes, for example the
     rootless Podman pause process (`runuser -u devcloud -- podman system migrate`, then `pkill -u`).
3. **PostgreSQL rename** *(ctl)*: `ALTER ROLE devcloud RENAME TO aifactory;
   ALTER DATABASE devcloud RENAME TO aifactory;` as the superuser, connected
   to `postgres`.
   - Native: `runuser -u postgres -- psql`.
   - Bundled container: start only the PostgreSQL container, run
     `podman exec … psql`, then stop it.
   - The sclorg image resets the password from `postgresql.env` on the next
     start, so an MD5 password lost by the rename is restored.
   - External PostgreSQL is not touched.
4. **Directories.** `rename(2)` each old root to its new name. Create
   compatibility symlinks from each old path to the new one.
5. **Account.**
   - `usermod -l aifactory -d /var/lib/aifactory devcloud` and
     `groupmod -n aifactory devcloud`.
   - Rewrite `/etc/subuid` and `/etc/subgid` if `usermod` did not.
6. **Symlinks inside the trees.**
   - Rewrite `/opt/aifactory/current`; its relative target goes through the
     old root.
   - Rewrite every `.venv` symlink under `releases/` whose absolute target
     starts with an old root (`engine.py:1158-1167, 1310`).
7. **Files.** Rename:
   - `aifactory.db` and its `-wal`/`-shm` files
   - `.devcloud-release-id` in every release
   - *(wrk)* the agent state files
8. **Env files.**
   - Rename the `DEVCLOUD_*` keys.
   - Replace old roots in the values.
   - *(ctl)* Rewrite `DATABASE_URL` for the bundled host and role/DB names,
     the native socket URL and the SQLite path. Rewrite `POSTGRESQL_USER`
     and `POSTGRESQL_DATABASE`.
9. **State.** Rewrite the roots and `service_user` in `install-state.json`.
   From here on, `detect_layout` returns `CURRENT`.
10. **Rootless Podman** *(native wrk)*.
    - Remove the libpod database (`storage/libpod/bolt_state.db` or
      `db.sql`). Images live in containers/storage and stay.
    - Check that `podman images` lists them.
    - If that fails, run `podman system reset --force`. The agent downloads
      the images again after it starts.
    - **Must be validated on a GPU test worker before release.**
11. **Platform images.** `podman tag` the current
    `localhost/devcloud-{controller,worker,postgresql}` images to the new
    names.
12. **Units.** The layout-aware engine renders and installs the new units,
    Quadlets, ingress helper and nginx conf. Then:
    - remove the old unit files, Quadlets, helper and `devcloud.conf`
    - `nginx -t`, then `systemctl daemon-reload`
    - remove the `devcloud` Podman network
    - if `devcloud-update.service` is running this command, leave its unit
      file and record it as deferred
13. **SELinux.**
    - Add the fcontext rule for the new workspace root.
    - `restorecon -R` the new roots.
    - Delete the old rule.
14. **`/etc/hosts`.** Rename the marker *(wrk)*.
15. **Start.** Start the new units. Health checks:
    - *(ctl)* the controller `/health` and the ingress
    - *(wrk)* `python -m app.installer.verify_worker`
16. **Finish.** Mark the journal complete. Print the deferred items and the
    note that the compatibility symlinks stay until release 2.

### Failure and resume
- If a step fails, the command undoes the completed steps in reverse order,
  starts the legacy units again, prints where it stopped and keeps the
  journal.
- If the process dies, `migrate-layout` finds an incomplete journal and asks
  whether to resume or undo (`--resume`/`--undo` for `--yes` runs).

### Why workspace containers are removed
- Their bind-mount sources are absolute old paths.
- On native workers, the rootless Podman database is reset anyway.
- Recreating them on the next start gives the new paths, the new container
  names and the new env names in one step, and leaves no container that
  depends on the symlinks.

## Release 2 changes

**Preflight**
- Host update: the detected layout must be `CURRENT`.
- Controller update: every enabled node must report release 1 or later and
  layout `aifactory` in its last heartbeat. Otherwise the update refuses and
  lists the nodes.

**Database migration 40 (one transaction)**
- `ALTER TABLE devcloud_schema_migrations RENAME TO aifactory_schema_migrations`
  if only the old one exists.
- Rewrite `localhost/devcloud-` to `localhost/aifactory-` in
  `workspace_images.image_ref` and in `custom_templates.image_tag`.
  Registry-hosted refs are left alone.
- Rewrite the `/var/lib/devcloud/` prefix in `workspaces.storage_path`.

**Code**
- New defaults in `templates.py` and the image build paths.
- `COOKIE_NAME="aifactory_session"` and share prefix `aifactory_share_`.
  Both old and new are still read and stripped.
- The GHCR default `ghcr.io/aydinguven/aifactory`.
- The update source URL rewrite (after Q5).

**CI**
- New names for every artifact and image.
- The old channel file is no longer updated.
- Dual-push to the old GHCR package is not needed, because bundles carry
  the images.

**Installer**
- On update, remove the compatibility symlinks once:
  - no symlink under the new roots points into an old root
  - no container mount source starts with an old root (`podman inspect`)
  - no deferred legacy unit is still running
- Remove the leftover `devcloud-update.service` file.

## Later cleanup release
- Remove:
  - the `DEVCLOUD_*` aliases and reading the old cookie name
  - the old migrations table read path and the old state path probe
  - the old artifact name acceptance and the `devcloud-setup.sh` shim
  - the old workspace env names and the `/_devcloud` redirect
- Keep:
  - stripping the old cookie names in the proxy (cheap, security)
  - restoring `devcloud-backup` archives

## Rollout runbook (goes into INSTALL.md)
1. Release 1 is published. Update the controller (UI or CLI). Workers
   update by OTA as usual. Nothing is renamed yet.
2. Check Admin > Worker'lar: every worker shows release 1, layout `devcloud`.
3. Controller window: run `migrate-layout` on the controller.
   - Workspaces keep running.
   - Users lose access only while the controller is down.
4. For each worker:
   - Stop its workspaces.
   - Run `migrate-layout` on that worker.
   - Start one workspace to check it.
   - The worker then shows layout `aifactory`.
5. Rename the Git repository (Q5).
6. Publish release 2 and update the controller, then the workers.

## Risks
- **Rootless Podman after the home move** (step 10) is the least certain
  step. It needs a real GPU worker test, including `podman system reset` and
  re-download time for the CUDA image.
- **Container-layer changes are lost** when workspace containers are
  recreated. Users must be told in advance.
- **`usermod -l` and `/etc/subuid`.** Whether `usermod -l` updates
  `/etc/subuid` differs between shadow-utils versions. The step checks and
  rewrites.
- **Separate mounts** under the old roots block the automatic move (Q2).
- **Admin-supplied overrides.** Custom roots in the state file that contain
  neither name, or an external `DATABASE_URL`, are left as they are and
  reported.
- **Cached `app.js`.** Browsers with a cached `app.js` that expects
  `devcloud_user_id` break until reload. The existing `static_version`
  cache-busting covers it.

## Testing
Following the test policy, only behaviour that can regress gets tests:
- `migrate-layout` on a fake host root (`host_path`, as in
  `tests/test_installer.py`):
  - the full run, which checks the resulting paths, units, env keys and
    state
  - a failure at a middle step, which checks that undo restores the legacy
    layout
  - resume after a stop
  - preflight refusals for a mount point and a running workspace
- Discovery and the update plan on both layouts; `from_dict` with unknown
  keys.
- The proxy strips both cookie names; the session is read from either name.
- The migrations-table helper on SQLite and PostgreSQL, before and after
  the rename.
- Worker image alias tagging in both directions; `alias_path`.
- Restore of a legacy backup on a new-layout host.
- Acceptance of both artifact name patterns.

Manual validation before release 1, on VMs:
- a container controller with bundled PostgreSQL
- a native controller, if Q1 says one exists
- a CPU container worker
- a native GPU worker
- an air-gapped update
- an injected failure that exercises undo
