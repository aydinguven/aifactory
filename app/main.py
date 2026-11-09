from contextlib import asynccontextmanager
import logging
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text, update

from app.auth.internal import hash_password
from app.config import settings
from app.database import AsyncSessionLocal
from app.task_health import task_health
from app.models.user import User, UserRole
from app.models.node import Node, NodeStatus
from app.proxy.router import proxy_router, model_endpoint_router
from app.routes.admin import admin_router
from app.routes.agent_routes import agent_router
from app.routes.auth_routes import auth_router
from app.routes.share_routes import share_router
from app.routes.download_routes import download_router
from app.routes.worker_bootstrap_routes import bootstrap_router
from app.routes.view_routes import view_router
from app.routes.workspace_routes import workspace_router
from app.routes.mlflow_routes import mlflow_router
from app.routes.image_release_routes import image_release_router
from app.routes.onboarding_routes import onboarding_router
from app.routes.genai_routes import genai_admin_router, genai_router
from app.routes.health_routes import health_router

# Setup logging
logging.basicConfig(
    level=logging.INFO if not settings.DEBUG else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("aifactory")


async def seed_initial_admin():
    """Seed initial administrator account if no admin exists."""
    async with AsyncSessionLocal() as db:
        stmt = select(User).where(User.username == settings.ADMIN_USERNAME)
        result = await db.execute(stmt)
        admin = result.scalar_one_or_none()

        if not admin:
            logger.info(f"Seeding default admin user: {settings.ADMIN_USERNAME}")
            admin_user = User(
                username=settings.ADMIN_USERNAME,
                email=settings.ADMIN_EMAIL,
                hashed_password=hash_password(settings.ADMIN_PASSWORD),
                full_name="Sistem Yöneticisi",
                role=UserRole.ADMIN,
                auth_source="internal",
                is_active=True,
            )
            db.add(admin_user)
            await db.commit()


async def mark_workers_offline_on_startup():
    """A persisted online state is stale until a worker reconnects to this process."""
    async with AsyncSessionLocal() as db:
        await db.execute(update(Node).values(status=NodeStatus.OFFLINE))
        await db.commit()


async def recover_interrupted_workspace_operations():
    """The single controller lost its in-flight commands when it restarted."""
    from app.models.workspace import Workspace, WorkspaceStatus
    async with AsyncSessionLocal() as db:
        await db.execute(update(Workspace).where(Workspace.status.in_([
            WorkspaceStatus.CREATING, WorkspaceStatus.STARTING, WorkspaceStatus.STOPPING,
        ])).values(status=WorkspaceStatus.ERROR,
                   error_message="Controller restarted during a workspace operation; retry start, stop, or delete."))
        await db.commit()


async def seed_bootstrap_worker() -> None:
    """Idempotently maintain the all-in-one host as an ordinary worker."""
    worker_id = settings.AIFACTORY_BOOTSTRAP_WORKER_ID.strip()
    if not worker_id:
        return
    token_hash = settings.AIFACTORY_BOOTSTRAP_WORKER_TOKEN_HASH.strip()
    async with AsyncSessionLocal() as db:
        node = await db.get(Node, worker_id)
        if node is None:
            node = Node(
                id=worker_id,
                name=(
                    settings.AIFACTORY_BOOTSTRAP_WORKER_NAME.strip()
                    or "all-in-one-worker"
                ),
                status=NodeStatus.PENDING,
            )
        node.agent_token_hash = token_hash
        node.enabled = True
        node.schedulable = True
        db.add(node)
        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler for startup and shutdown."""
    import asyncio
    from app.orchestrator.idle_reaper import idle_reaper_background_worker
    from app.orchestrator.mlflow_deployment_service import (
        mlflow_deployment_background_worker,
        recover_interrupted_mlflow_deployments,
    )
    from app.orchestrator.image_release_service import image_release_background_worker
    from app.migrations import require_current, upgrade
    from app.directory_sync import directory_sync_background_worker

    logger.info("Initializing AI Factory Database...")
    if settings.AUTO_MIGRATE:
        await upgrade()
    else:
        await require_current()
    await seed_bootstrap_worker()
    await mark_workers_offline_on_startup()
    await recover_interrupted_workspace_operations()
    await recover_interrupted_mlflow_deployments()
    await seed_initial_admin()
    logger.info("AI Factory controller ready; workspace execution is worker-only.")

    # Enforce runtime policies and advance durable model deployment jobs.
    reaper_task = asyncio.create_task(idle_reaper_background_worker(check_interval_seconds=60))
    deployment_task = asyncio.create_task(
        mlflow_deployment_background_worker(check_interval_seconds=2)
    )
    image_release_task = asyncio.create_task(
        image_release_background_worker(check_interval_seconds=2)
    )
    # Place every AD team under its müdürlük without waiting for logins.
    directory_sync_task = asyncio.create_task(
        directory_sync_background_worker(
            AsyncSessionLocal, interval_hours=settings.DIRECTORY_SYNC_INTERVAL_HOURS
        )
    )
    # Stale windows allow for slow iterations: a reaper cycle stops containers
    # through workers and one deployment iteration can wait for a model build.
    task_health.register(
        "idle_reaper", "Çalışma süresi sınırlayıcı", reaper_task,
        stale_after_seconds=5 * 60,
    )
    task_health.register(
        "mlflow_deployments", "Model dağıtım kuyruğu", deployment_task,
        stale_after_seconds=settings.MLFLOW_MODEL_BUILD_TIMEOUT_SECONDS + 10 * 60,
    )
    # One release can wait for a model build and then a registry copy.
    task_health.register(
        "image_releases", "Kubernetes imaj yayın kuyruğu", image_release_task,
        stale_after_seconds=settings.MLFLOW_MODEL_BUILD_TIMEOUT_SECONDS
        + settings.WORKSPACE_IMAGE_IMPORT_TIMEOUT_SECONDS
        + 10 * 60,
    )
    task_health.register(
        "directory_sync", "AD senkronizasyonu", directory_sync_task,
        stale_after_seconds=60 + 3 * settings.DIRECTORY_SYNC_INTERVAL_HOURS * 3600,
        enabled=settings.DIRECTORY_SYNC_INTERVAL_HOURS > 0,
    )

    yield
    directory_sync_task.cancel()
    reaper_task.cancel()
    deployment_task.cancel()
    image_release_task.cancel()
    await asyncio.gather(
        reaper_task,
        deployment_task,
        image_release_task,
        directory_sync_task,
        return_exceptions=True,
    )
    logger.info("AI Factory shutting down...")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Self-hosted Cloud Development Environment Platform (Podman Orchestration)",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
)


@app.get("/healthz", include_in_schema=False)
async def healthcheck():
    """Process liveness endpoint for systemd and OCI health checks."""
    return {"status": "ok", "version": settings.APP_VERSION}


@app.get("/readyz", include_in_schema=False)
async def readiness_check():
    """Readiness requires the controller to reach its configured database.

    A pooled connection can keep `SELECT 1` succeeding while new connections
    fail, so the database host must also still resolve.
    """
    import asyncio

    from sqlalchemy.engine import make_url

    try:
        url = make_url(settings.DATABASE_URL)
        if url.host:
            await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(url.host, url.port or 5432),
                timeout=3,
            )
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        logger.warning("Readiness check failed: %s: %s", type(exc).__name__, exc)
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
                "reason": type(exc).__name__,
                "version": settings.APP_VERSION,
            },
        )
    return {"status": "ready", "version": settings.APP_VERSION}

@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Keep API errors machine-readable; the traceback is still logged by Starlette."""
    logger.error(
        "Unhandled %s on %s %s", type(exc).__name__, request.method, request.url.path
    )
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            status_code=500,
            content={"detail": "Beklenmeyen sunucu hatası. Ayrıntılar sunucu günlüğünde."},
        )
    return PlainTextResponse("Internal Server Error", status_code=500)


# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# Include routers
from app.routes.file_routes import file_router

app.include_router(auth_router)
app.include_router(workspace_router)
app.include_router(file_router)
app.include_router(admin_router)
app.include_router(agent_router)
app.include_router(mlflow_router)
app.include_router(image_release_router)
app.include_router(onboarding_router)
app.include_router(genai_router)
app.include_router(genai_admin_router)
app.include_router(share_router)
app.include_router(model_endpoint_router)
app.include_router(proxy_router)
app.include_router(download_router)
app.include_router(bootstrap_router)
app.include_router(health_router)
app.include_router(view_router)
