# MLflow → Kubernetes (Nexus image publishing) — Requirements

## Context

Agreed in the "Mlflow Build Hk." mail thread (October 2026):

- The YZ team registers and versions models in MLflow and starts the process
  from AI Factory, the same way as AI Factory deploys today.
- AI Factory builds the serving image from an MLflow model version, names it by
  the institutional standard and pushes it to the deploy registry (Nexus)
  `btgvalmbxp1:8525` as `aimldeployer`. The password was sent to Aydin
  separately and is entered only in the admin UI.
- Image name: `rfskodu/uygulama-tamadı`, e.g. `mbnllmd/mbnllmdml-cashflowai`.
  Bitbucket uses the same names in upper case (project `MBNLLMD`, repo
  `MBNLLMDML-SWIFTANOMALY`; `MBNLLMDPY-SWIFTANOMALY` was renamed to fit).
- Version: AI Factory sets only `X.Y.Z` (semver). The platform team (Hakkı
  Bozkurt) re-tags and re-deploys: `X.Y.Z-RC.BN` in test, `X.Y.Z-BN` in prod.
- Secrets: Vault → External Secrets Operator (authorized ServiceAccount) →
  Kubernetes Secret → Deployment `envFrom`. Environment config is in Helm.
  AI Factory never handles these values, and images carry no secrets.
- First consumer: Payguard Anomaly Detection. MLflow `PayguardAnomalyUnified`
  v3 → `mbnllmd/mbnllmdml-swiftanomaly:1.0.0`. The model reads `DB_HOST`,
  `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASS` and `DB_SCHEMA` when the service
  starts, and does not connect to the database during registration or build.
- The deploy modal already shows a disabled Kubernetes target
  (`DEPLOY_TARGETS.kubernetes.available = false`).

Phase 1 publishes the image to Nexus. Phase 2 triggers the platform deploy
workflow, once the platform team says how.

## Requirements

### R1 — Deploy registry (admin)
1. A new admin section, **Yönetim → Kubernetes**, has a **Dağıtım Registry'si
   (Nexus)** card with these fields:
   - enabled;
   - registry `host:port` (no scheme, no path);
   - username and password;
   - **TLS sertifikasını doğrula** (on by default). When it is off, the
     certificate is not checked and plain HTTP is allowed. No CA file is
     needed.
   - manifest format: keep the source manifest, or convert it to Docker v2
     schema 2.
2. The password is stored encrypted and never returned (`has_password` only).
   `password: null` keeps it; `""` clears it.
3. A **Test** button logs in to the registry from the controller. It follows
   the Docker Bearer token challenge that Nexus uses.
4. It is separate from the Model Container Registry, which stays the
   intermediate registry for builds. AI Factory never deletes anything in the
   deploy registry.

### R2 — Kubernetes projects (admin) and applications (self-service)
1. The same **Yönetim → Kubernetes** section lists the projects as **RFS
   Kodları**. Only admins create projects.
2. Project fields: RFS code (lowercase, e.g. `mbnllmd`), display name,
   namespace (DNS-1123 label, unique; defaults to the RFS code) and enabled.
   A project has one or more applications (one ML service = one container =
   one application).
3. Applications are created by their first release from the deploy modal
   (R4.1), not in the admin section. Fields: app name (e.g.
   `mbnllmdml-swiftanomaly`, must start with the RFS code), the bound MLflow
   registered model name (the model of that first release), an optional
   Bitbucket repo URL, and enabled. The image repository is
   `{rfs_code}/{app_name}` and is unique. Admins can disable, delete
   (without releases) or rebind an application to another model.
4. After a project or application has a published release:
   - its namespace, RFS code and app name are read-only;
   - it can be disabled but not deleted.

### R3 — Who can publish
1. Every Active Directory user (`auth_source == "active_directory"`) and every
   admin. One check function, so it can later be limited to project members.

### R4 — Publish flow
1. In the MLflow deploy modal, the user picks the Kubernetes target, an
   enabled RFS code, and types the app name; an application already bound to
   the model is prefilled. An unknown name is created with the release and
   bound to the model. An existing application bound to another model is
   refused. Namespace and image repository are shown read-only. The user
   then enters `X.Y.Z`.
2. The version is strictly `X.Y.Z`, with no pre-release or build suffix. The
   suggested value is the application's highest published version with
   patch + 1, or `1.0.0` if there is none.
3. A release is refused if the same application and version already has a
   release that is not failed, or if the tag already exists in the deploy
   registry. AI Factory never overwrites a tag on purpose. The Nexus hosted
   repository must have "Disable redeploy" enabled, to rule out a push that
   lands between the check and the copy.
4. The model version is read with the user's MLflow credentials, as AI
   Factory deploys do.
5. A ready build of the same model version is reused. Otherwise the image is
   built on a worker with the existing pipeline.
6. The controller copies the image to `{registry}/{rfs_code}/{app_name}:{X.Y.Z}`
   and records the digest. Deploy registry credentials never leave the
   controller.
7. Progress is shown as an event log, as for AI Factory deployments. The
   result shows the full image reference and digest, with a note that the
   platform team re-tags and deploys it.
8. A failed release can be retried with the same version.
9. After a successful publish, AI Factory sets `aifactory.image` and
   `aifactory.image_digest` tags on the MLflow model version. If setting the
   tags fails, it is logged as a warning and the release still succeeds.
10. Images built only for publishing are not added to the worker image
    catalog, so workers do not sync them. They are removed from the
    intermediate registry when nothing uses them anymore.

### R5 — Listing
1. The Deployments page lists Kubernetes releases with the **Kubernetes**
   badge. Columns: application, namespace, model and version, image version,
   status, user, date. Users see their own releases; admins see all.
2. The modal shows the last published version of the selected application.

### R6 — Phase 2 (waiting on the platform team)
1. After publishing, trigger the platform workflow with the image reference,
   digest, version, namespace, application and model. The mechanism is not
   decided yet.
2. Show workflow or deploy status if the platform exposes it.

## Out of scope
- Vault, External Secrets, Helm values and Kubernetes manifests. These belong
  to the platform team; Payguard's drafts are in its Bitbucket repo.
- RC/BN tagging and promotion to prod.
- Running these images on the AI Factory target with database variables. A
  model that connects at startup may not become healthy there, and publishing
  does not depend on it.

## Open questions
1. (Hakkı) What is the experiment naming standard? Mustafa asked and it is not
   answered yet. Proposal: experiment name = app name.
2. (Hakkı) Is the rule "RFS code = lowercase Bitbucket project key, app name =
   lowercase repo name"? The app-name prefix is now validated against the
   RFS code; deriving both from the repo is still open.
3. (Hakkı) How should the platform pipeline learn about a new image (webhook,
   Jenkins, ArgoCD, Jira), and with which payload? Needed for Phase 2.
4. (Hakkı) Deploy registry details:
   - whether 8525 is the push port;
   - what `aimldeployer` may push to;
   - the redeploy policy;
   - whether OCI manifests are accepted. If not, select v2s2 in the admin
     card.

   A CA is no longer needed: TLS checking can be turned off.
5. (Hakkı) Who creates namespaces, and how are they named (e.g. `mbnllmd`)?
6. (Payguard) Is `mbnllmd/mbnllmdml-swiftanomaly:1.0.0` already in Nexus? Is
   the PostgreSQL driver listed in the MLflow model's requirements?

## Non-functional
- Turkish UI text.
- Works on SQLite and PostgreSQL. Status columns are strings, so no
  PostgreSQL enum changes are needed.
- Secrets are redacted from logs and events. Temporary auth files are 0600 and
  deleted.
- No worker upgrade is needed: the worker build RPC is unchanged.
