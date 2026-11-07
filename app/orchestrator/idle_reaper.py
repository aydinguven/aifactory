import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy import select, update

from app.database import AsyncSessionLocal
from app.models.workspace import Workspace, WorkspaceStatus
from app.models.mlflow_deployment import MlflowDeployment, MlflowDeploymentStatus
from app.orchestrator.runtime_backend import runtime_for_node
from app.orchestrator.admission import admission_transaction
from app.task_health import task_health
from app.time_utils import ensure_utc

logger = logging.getLogger("devcloud.reaper")


async def run_idle_reaper_cycle() -> int:
    """Enforce maximum runtime since start, regardless of IDE or job activity."""
    stopped_count = 0
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        stmt = select(Workspace).where(
            Workspace.status == WorkspaceStatus.RUNNING,
            Workspace.auto_stop_minutes > 0,
        )
        result = await db.execute(stmt)
        workspaces = result.scalars().all()

        for ws in workspaces:
            # Claim the transition under the same lock used by manual starts and
            # stops, then release the database before worker I/O.
            async with admission_transaction(db):
                await db.refresh(ws)
                if ws.status != WorkspaceStatus.RUNNING or not ws.last_started_at or not ws.auto_stop_minutes:
                    continue
                elapsed_minutes = (now - ensure_utc(ws.last_started_at)).total_seconds() / 60.0
                if elapsed_minutes < ws.auto_stop_minutes:
                    continue
                ws.status = WorkspaceStatus.STOPPING
            logger.info("Stopping workspace %s at its %sm maximum runtime", ws.id, ws.auto_stop_minutes)
            try:
                if not await runtime_for_node(ws.node_id).stop_container(ws.container_name):
                    raise RuntimeError("Worker refused stop; preserving workspace state")
                ws.status = WorkspaceStatus.STOPPED
                ws.error_message = None
                ws.last_stopped_at = now
                await db.execute(
                    update(MlflowDeployment)
                    .where(MlflowDeployment.workspace_id == ws.id)
                    .values(
                        status=MlflowDeploymentStatus.STOPPED,
                        status_message="Maksimum çalışma süresi dolduğu için servis durduruldu.",
                        error_message=None,
                    )
                )
                stopped_count += 1
            except Exception as exc:
                ws.status = WorkspaceStatus.RUNNING
                ws.error_message = str(exc)
                logger.error("Error stopping workspace %s: %s", ws.id, exc)
            await db.commit()

    return stopped_count


async def idle_reaper_background_worker(check_interval_seconds: int = 60) -> None:
    """Enforce the configured runtime cap; historical function name is retained."""
    logger.info("AI Factory Maximum Runtime Reaper background worker started.")
    while True:
        try:
            await asyncio.sleep(check_interval_seconds)
            await run_idle_reaper_cycle()
            task_health.success("idle_reaper")
        except asyncio.CancelledError:
            logger.info("Maximum Runtime Reaper received cancellation. Shutting down.")
            break
        except Exception as exc:
            task_health.failure("idle_reaper", exc)
            logger.error(f"Unexpected error in idle reaper loop: {exc}")
