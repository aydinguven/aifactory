import asyncio
import atexit
import json
import os
import shutil
import tempfile
from types import SimpleNamespace
from pathlib import Path

# Keep test runs out of the developer's checkout and independent of local
# configuration. These must be set before app.config is imported: Settings()
# is built at import time.
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="aifactory-tests-"))
atexit.register(shutil.rmtree, _TEST_ROOT, ignore_errors=True)
os.environ["AIFACTORY_ENV_FILE"] = ""  # ignore any local .env
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_TEST_ROOT / 'aifactory.db'}"
os.environ["STORAGE_ROOT"] = str(_TEST_ROOT / "workspaces")
os.environ["DOWNLOAD_BUILD_ROOT"] = str(_TEST_ROOT / "download-builds")
os.environ["BCRYPT_ROUNDS"] = "4"  # cost 12 took ~60% of suite time

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.config import settings
from app.database import Base, get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.models.node import Node, NodeStatus
from app.models.custom_template import CustomTemplate
from app.models.directory_settings import DirectorySettings
from app.models.jupyter_ai_settings import JupyterAiSettings
from app.main import app
from app.orchestrator.podman_service import podman_service
from app.agents.manager import AgentConnection, agent_manager
from app.worker_agent import WorkerAgent

# Force test mode and mock podman
settings.USE_MOCK_PODMAN = True
podman_service._mock_mode = True

# Test in-memory database
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
test_engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
)
TestingSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

TEST_WORKER_ID = "00000000-0000-0000-0000-000000000001"


class InProcessWorkerConnection(AgentConnection):
    """Exercise the production worker command surface without a real socket."""

    def __init__(self, agent: WorkerAgent, db_session: AsyncSession):
        super().__init__(TEST_WORKER_ID, None)
        self.agent = agent
        self.db_session = db_session
        self.tasks = set()
        self.agent.websocket = SimpleNamespace(send=self.deliver)

    async def _ensure_registry(self, payload: dict) -> None:
        container_name = str(payload.get("container_name") or "")
        if not container_name or container_name in self.agent.registry:
            return
        from sqlalchemy import select

        workspace = (
            await self.db_session.execute(
                select(Workspace).where(Workspace.container_name == container_name)
            )
        ).scalar_one_or_none()
        if workspace:
            self.agent.registry[container_name] = {
                "workspace_id": workspace.id,
                "container_id": workspace.container_id or "",
                "storage_path": workspace.storage_path,
                "host_port": workspace.host_port,
            }

    async def deliver(self, raw):
        await self.handle_message(json.loads(raw))

    async def send_json(self, message):
        if message["type"] == "command":
            await self._ensure_registry(message.get("payload") or {})
            task = asyncio.create_task(self.agent.handle_command(message))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        else:
            await self.agent.handle_stream_message(message)

    async def cleanup(self):
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.agent.transfers.expire(all=True)


@pytest_asyncio.fixture(scope="function")
async def db_session():
    """Create fresh database tables for each test function."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with TestingSessionLocal() as session:
        yield session

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture(scope="function")
async def client(db_session: AsyncSession, tmp_path: Path):
    """Async HTTP test client overriding get_db dependency."""
    async def override_get_db():
        yield db_session

    worker = Node(
        id=TEST_WORKER_ID,
        name="test-worker",
        status=NodeStatus.ONLINE,
        cpu_total=64,
        memory_total_mb=262144,
        disk_total_mb=1048576,
    )
    db_session.add(worker)
    await db_session.commit()
    agent = WorkerAgent()
    agent.registry_path = tmp_path / "worker-registry.json"
    agent.registry = {}
    connection = InProcessWorkerConnection(agent, db_session)
    agent_manager._connections[TEST_WORKER_ID] = connection

    app.dependency_overrides[get_db] = override_get_db
    
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    await connection.cleanup()
    app.dependency_overrides.clear()
    if agent_manager._connections.get(TEST_WORKER_ID) is connection:
        agent_manager._connections.pop(TEST_WORKER_ID, None)
