# Rename technical identifiers from devcloud to aifactory — Tasks

## 3.49.1 — pre-migration

- [x] 1. Rename the technical names in code, scripts, templates, images,
  CI and tests. Keep the names listed under "Kept until 3.50.1" in
  `design.md`.
- [x] 2. Read both names for:
  - bundle, archive and offline names;
  - the channel file;
  - the delegation and progress env;
  - the release marker;
  - the image cleanup;
  - the `/etc/hosts` marker;
  - cookies (read, strip, clear on logout);
  - the migrations table;
  - the worker image twins;
  - the home sentinel;
  - the saved MLflow filters;
  - the status route.
- [x] 3. `app/installer/layout.py` and `app/installer/legacy_layout.py`:
  detection, the steps, the journal, undo, commit and recovery, and the
  update watcher handover.
- [x] 4. Engine and CLI integration (`build_update_plan(migration=...)`,
  legacy configuration, other commands refused on a devcloud host).
- [x] 5. Backup with a layout; restore of devcloud backups.
- [x] 6. CI publishes `aifactory-update-channel.json` and
  `devcloud-update-channel.json`.
- [x] 7. Tests (`tests/test_legacy_layout.py`, cookie forwarding).
- [x] 8. Docs: new names, plus "Moving from the devcloud names" in INSTALL.md.
  `DEVELOPMENT.md`.
- [x] 9. Version 3.49.1.

## Rollout of 3.49.1

- [x] 10. Release 3.49.1, then 3.49.2 (the 3.49.1 database rename failed on the controller; see DEVELOPMENT.md).
- [x] 11. Controller: Admin > System > Platform Güncelleme. Check:
  - `/etc/aifactory`, `/var/lib/aifactory` and `/srv/aifactory-downloads`
    exist, and the old paths are symlinks;
  - `systemctl status aifactory-controller aifactory-postgresql
    aifactory-ingress.path aifactory-update.path` shows the units running;
  - login works and the old session is kept;
  - the worker bundle is listed for workers.
- [x] 12. Worker: stop its workspaces, then OTA from Admin > Worker'lar.
  Check:
  - the worker reconnects on 3.49.1;
  - starting a workspace recreates its container with the project files.
- [x] 13. Rename the Git repository (Aydin): `aydinguven/aifactory`.

## 3.50.1 — post-migration

- [x] 14. Preflight: the host is on the aifactory layout. On the controller,
  every enabled node reports 3.49.1 or later.
- [x] 15. Migration 39:
  - rename the migrations table;
  - rewrite the `localhost/devcloud-` image refs;
  - rewrite the `storage_path` prefix.
- [x] 16. Switch the remaining names:
  - workspace image tags (templates, builds, CI, offline);
  - the cookie name and share prefix;
  - bundle, archive, offline and keyring asset names;
  - the GHCR default and the update source URL.
  CI stops updating `devcloud-update-channel.json`.
- [x] 17. Remove the compatibility symlinks once no link or container mount
  points into them. Remove a leftover `devcloud-update.service`.
- [x] 18. Tests for migration 39 and the symlink checks, then the docs.

- [ ] 18a. Release 3.50.1; update the controller, then the worker.

## Later

- [ ] 19. Remove the old-name fallbacks:
  - env names and the old cookie read;
  - the old migrations table read and the old state probe;
  - old artifact names;
  - the `DEVCLOUD_WORKSPACE_ID`/`DEVCLOUD_USER_ID` injection;
  - the `/_devcloud` alias and the legacy module.

  Keep stripping the old cookie names, and keep restoring devcloud backups.
