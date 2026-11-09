# Rename technical identifiers from devcloud to aifactory — Tasks

## Phase 0 — decisions and checks (no code)

- [ ] 0.1 Answer Q1–Q7 in `requirements.md`.
- [ ] 0.2 On each host, record the controller runtime, database mode, worker
  runtime and any mounts under the old roots (`findmnt -R /var/lib/devcloud`,
  `findmnt /opt/devcloud`, `findmnt /srv/devcloud-downloads`).
- [ ] 0.3 On a throwaway native GPU worker VM, try the rootless Podman step
  by hand: rename the user and home, remove the libpod DB, check
  `podman images`, then start a workspace container. Record the result in
  `design.md`.

## Release 1 (4.0.0) — read both names, add `migrate-layout`

- [ ] 1. `app/naming.py`: `LEGACY` and `CURRENT` name sets,
  `detect_layout`, `env()` and `alias_path()`.
- [ ] 2. Installer layout support:
  - Route every path, unit, Quadlet, container, network, image, marker and
    env key in `app/installer/*` and `deploy/*.sh` through the layout.
  - Add template placeholders.
  - Rename the template files and the setup script, and add the
    `devcloud-setup.sh` shim.
  - Fresh installs use `CURRENT`.
- [ ] 3. Installer compatibility:
  - state discovery on both paths
  - the path guard accepts both names
  - `from_dict` and `load` ignore unknown keys
  - secret re-read accepts both key names
  - both names of the delegation and progress env vars
  - both keyring paths
  - both bundle and archive name patterns, and both release markers
  - image cleanup for both prefixes
  - `controller.env` always lists the explicit roots
- [ ] 4. Backup and restore: write the layout's archive names, restore both
  layouts, and rewrite paths, user and DB names on restore.
- [ ] 5. Ingress: `apply_ingress.py` and `install_ingress.sh` take their
  paths from the layout.
- [ ] 6. Worker bootstrap: `install_worker.sh` and `deploy_worker_offline.sh`
  accept both bundle roots, both setup script names, both env names and both
  state files.
- [ ] 7. Settings: rename the fields with `AliasChoices`, and keep the old
  `COOKIE_NAME` default.
- [ ] 8. Cookies:
  - read the session from either name
  - read share cookies under either prefix
  - the proxy strips both
- [ ] 9. Migrations-table helper used by `migrations.py` and `health.py`.
- [ ] 10. Workspace names: new workspaces get `aifactory-…`, and a
  recreated `devcloud-…` container gets a new name.
- [ ] 11. Worker agent:
  - `naming.env` for every env read
  - state files under both names
  - image alias tagging
  - `alias_path` for controller paths
  - `layout` in the heartbeat
  - internal workspace env names renamed
  - both names injected for `WORKSPACE_ID`/`USER_ID`
- [ ] 12. Controller: store and show the worker layout in
  Admin > Worker'lar.
- [ ] 13. Images:
  - rename the paths inside the images, the tmux session and the profile
    script
  - the terminal entrypoint checks both home sentinels
  - the skills directory is moved to `.aifactory`
- [ ] 14. Internal names:
  - logger names, temp prefixes, DOM events
  - localStorage migration
  - the `/_aifactory/status` route with a redirect from `/_devcloud`
  - JSON field names
  - LiteLLM metadata and the alias prefix for new objects
- [ ] 15. `migrate-layout` command:
  - preflight checks
  - journal
  - steps 1–16 with undo
  - resume and undo options
  - "already migrated" detection
  - CLI and interactive menu entries
- [ ] 16. CI: publish `aifactory-update-channel.json` next to the old file;
  keep the other artifact names.
- [ ] 17. Tests from the "Testing" section of `design.md`.
- [ ] 18. Docs: new names for fresh installs, plus the INSTALL.md
  "Migrating from devcloud names" runbook. Update `DEVELOPMENT.md`.
- [ ] 19. Manual validation on VMs (the list in `design.md`), including undo.
- [ ] 19a. Release bump to 4.0.0 in a separate release PR, as usual.

## Rollout of release 1

- [ ] 20. Update the controller and the workers to 4.0.0.
- [ ] 21. Run `migrate-layout` on the controller, then on each worker after
  stopping its workspaces. Track progress per host in `DEVELOPMENT.md`.
- [ ] 22. Rename the Git repository (Aydin).

## Release 2 (4.1.0) — switch over

- [ ] 23. Update preflight: the host must be on `CURRENT`. The controller
  update requires every enabled node to be on 4.0.0 or later with layout
  `aifactory`.
- [ ] 24. Migration 40:
  - rename the migrations table
  - rewrite `localhost/devcloud-` image refs
  - rewrite the `storage_path` prefix
- [ ] 25. Code defaults: workspace image tags, cookie name and share prefix,
  GHCR default, update source URL rewrite.
- [ ] 26. CI: new artifact and image names, and stop updating the old
  channel file.
- [ ] 27. Installer: remove the compatibility symlinks after the checks, and
  remove the leftover legacy update unit.
- [ ] 28. Tests for migration 40 and the symlink removal checks. Update the
  docs.
- [ ] 28a. Release bump to 4.1.0 in a separate release PR.

## Later cleanup

- [ ] 29. Remove the old-name fallbacks listed in `design.md`, "Later cleanup
  release".
