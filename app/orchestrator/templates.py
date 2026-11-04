from dataclasses import dataclass, field
from app.schemas.workspace import TemplateInfo
from app.kilo_code import KILO_CODE_ASSISTANT


@dataclass(frozen=True)
class WorkspaceTemplate:
    id: str
    name: str
    description: str
    category: str
    icon: str
    default_port: int
    container_workdir: str
    image_tag: str
    features: list[str]
    ide_type: str
    env_vars: dict[str, str] = field(default_factory=dict)
    startup_command: list[str] = field(default_factory=list)
    mount_workspace: bool = True
    health_path: str = ""
    require_ready: bool = False
    # VS Code AI assistant wiring: "" keeps the legacy Cline injection.
    ai_assistant: str = ""
    # Retired templates keep running their existing workspaces but are hidden
    # from the catalog and refused for new workspaces.
    retired: bool = False
    # GPU-only images: refused on CPU flavors and synchronized only to workers
    # that report an NVIDIA stack.
    requires_accelerator: bool = False
    # First worker release that knows this template. An older worker would
    # register it as a custom template and lose its built-in wiring.
    min_agent_version: str = ""

    def to_schema(self) -> TemplateInfo:
        return TemplateInfo(
            id=self.id,
            name=self.name,
            description=self.description,
            category=self.category,
            icon=self.icon,
            default_port=self.default_port,
            image_tag=self.image_tag,
            features=self.features,
            requires_accelerator=self.requires_accelerator,
        )


TEMPLATES: dict[str, WorkspaceTemplate] = {
    "vscode-kilo": WorkspaceTemplate(
        id="vscode-kilo",
        name="Kilo Code",
        description="Python 3, uv, Node.js 22 ve Kilo Code yapay zekâ asistanı yüklü VS Code ortamı.",
        category="Genel",
        icon="code-bracket-square",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-kilo:latest",
        features=[
            "VS Code Web (code-server)",
            "Kilo Code AI Asistanı",
            "Skill, agent ve kural kataloğu",
            "Kod tabanı indeksleme",
            "Python 3 & uv",
            "Python, Jupyter, basedpyright & Ruff eklentileri",
            "Node.js 22 LTS",
            "Git & Terminal",
        ],
        ide_type="vscode",
        env_vars={"DOCKER_USER": "coder"},
        ai_assistant=KILO_CODE_ASSISTANT,
    ),
    "vscode-cuda": WorkspaceTemplate(
        id="vscode-cuda",
        name="Kilo Code (CUDA)",
        description="GPU worker'lar için CUDA 13, cuDNN ve NCCL yüklü Kilo Code VS Code ortamı. GPU kaynak profili gerektirir.",
        category="GPU",
        icon="cpu-chip",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-cuda:latest",
        features=[
            "NVIDIA CUDA 13 Toolkit",
            "cuDNN 9 & NCCL",
            "VS Code Web (code-server)",
            "Kilo Code AI Asistanı",
            "Python 3 & uv",
            "Python, Jupyter, basedpyright & Ruff eklentileri",
            "Node.js 22 LTS",
        ],
        ide_type="vscode",
        env_vars={"DOCKER_USER": "coder"},
        ai_assistant=KILO_CODE_ASSISTANT,
        requires_accelerator=True,
        min_agent_version="3.19.0",
    ),
    # Retired in 3.18.0 in favour of the Kilo Code workspace. Kept so existing
    # workspaces still start; hidden from the catalog and refused for new workspaces.
    "vscode-empty": WorkspaceTemplate(
        id="vscode-empty",
        name="Boş Proje",
        description="Tarayıcıda terminal ve Git içeren temiz, standart VS Code ortamı.",
        category="Genel",
        icon="code-bracket",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-empty:latest",
        features=["VS Code Web (code-server)", "Cline AI Assistant", "Git & Terminal", "Boş ve temiz çalışma alanı"],
        ide_type="vscode",
        env_vars={"DOCKER_USER": "coder"},
        retired=True,
    ),
    "vscode-python": WorkspaceTemplate(
        id="vscode-python",
        name="Python 3.14",
        description="Python 3.14/3.12, pip, uv ve VS Code Python extension yüklü geliştirme ortamı.",
        category="Python",
        icon="code-bracket-square",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-python:latest",
        features=["Python 3.14 Runtime", "VS Code Python Extension", "Cline AI Assistant", "uv & pip", "Jupyter Interactive Extension"],
        ide_type="vscode",
        env_vars={"DOCKER_USER": "coder"},
        retired=True,
    ),
    "vscode-react": WorkspaceTemplate(
        id="vscode-react",
        name="React/Node.js",
        description="Node.js 22 LTS, package manager ve React/TypeScript araçları yüklü VS Code ortamı.",
        category="Web Geliştirme",
        icon="code-bracket-square",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-react:latest",
        features=["Node.js 22 LTS", "React & TypeScript Tooling", "Cline AI Assistant", "npm, pnpm & Yarn", "ESLint & Prettier Extensions"],
        ide_type="vscode",
        env_vars={"DOCKER_USER": "coder"},
        retired=True,
    ),
    "jupyter-python": WorkspaceTemplate(
        id="jupyter-python",
        name="Jupyter Notebook",
        description="Python, veri bilimi paketleri ve Claude ACP içeren Jupyter AI ortamı.",
        category="Veri Bilimi",
        icon="chart-bar",
        default_port=8888,
        container_workdir="/home/jovyan/work",
        image_tag="localhost/devcloud-jupyter-python:latest",
        features=["JupyterLab & Notebooks", "Jupyter AI", "Claude ACP", "Veri bilimi araçları"],
        ide_type="jupyter",
        env_vars={"JUPYTER_ENABLE_LAB": "yes"},
    ),
    "terminal-rocky": WorkspaceTemplate(
        id="terminal-rocky",
        name="Terminal (Rocky Linux 10)",
        description="Tarayıcıda çalışan yerel Rocky Linux 10 kabuğu; tmux ile kalıcı oturum.",
        category="Sistem",
        icon="command-line",
        default_port=7681,
        # The whole home directory persists, so shell history, dotfiles and
        # user-installed tooling survive a restart.
        container_workdir="/home/devuser",
        image_tag="localhost/devcloud-terminal-rocky:latest",
        features=[
            "Rocky Linux 10 kabuğu",
            "tmux ile kalıcı oturum",
            "Git, Python 3, derleyici araçları",
            "Kalıcı ev dizini",
        ],
        # A dedicated ide_type keeps the image's own ENTRYPOINT in charge: no
        # entrypoint override, no injected IDE environment, no startup command.
        ide_type="terminal",
        health_path="/",
    ),
    "vscode-java": WorkspaceTemplate(
        id="vscode-java",
        name="Java 21 LTS",
        description="OpenJDK 21, Maven, Gradle ve Red Hat Java Language Support yüklü VS Code ortamı.",
        category="Java",
        icon="command-line",
        default_port=8080,
        container_workdir="/home/coder/project",
        image_tag="localhost/devcloud-vscode-java:latest",
        features=["OpenJDK 21 LTS", "Language Support for Java", "Cline AI Assistant", "Maven & Gradle", "Java Debugger Extension"],
        ide_type="vscode",
        env_vars={"DOCKER_USER": "coder"},
        retired=True,
    ),
}

INTERNAL_TEMPLATE_IDS = {"mlflow-serving"}
TEMPLATES["mlflow-serving"] = WorkspaceTemplate(
    id="mlflow-serving",
    name="MLflow Model Service",
    description="Immutable MLflow model version served as an authenticated HTTP endpoint.",
    category="Internal",
    icon="server-stack",
    default_port=8080,
    container_workdir="/",
    image_tag="",
    features=["MLflow scoring server", "Authenticated inference endpoint"],
    ide_type="service",
    mount_workspace=False,
    health_path="/ping",
    require_ready=True,
)

BUILTIN_TEMPLATE_IDS = tuple(
    template_id for template_id in TEMPLATES if template_id not in INTERNAL_TEMPLATE_IDS
)


def get_template(template_id: str) -> WorkspaceTemplate | None:
    return TEMPLATES.get(template_id)


def is_retired(template_id: str) -> bool:
    template = TEMPLATES.get(template_id)
    return bool(template and template.retired)


def requires_accelerator(template_id: str) -> bool:
    template = TEMPLATES.get(template_id)
    return bool(template and template.requires_accelerator)


def flavor_mismatch(template, flavor) -> str:
    """Explain why a template cannot run on a flavor, else ''."""
    if getattr(template, "requires_accelerator", False) and not flavor.accelerator_count:
        return (
            f"'{template.name}' şablonu GPU kaynak profili gerektirir; "
            "GPU Hızlandırma bölümünden bir profil seçin."
        )
    return ""


def list_templates() -> list[TemplateInfo]:
    return [
        template.to_schema()
        for template_id, template in TEMPLATES.items()
        if template_id not in INTERNAL_TEMPLATE_IDS and not template.retired
    ]


def list_builtin_templates() -> list[TemplateInfo]:
    return [
        TEMPLATES[template_id].to_schema()
        for template_id in BUILTIN_TEMPLATE_IDS
        if not TEMPLATES[template_id].retired
    ]


def register_custom_template(
    template_id: str,
    name: str,
    description: str,
    category: str,
    image_tag: str,
    default_port: int = 8080,
    ide_type: str = "vscode",
    icon: str = "cube",
    mount_workspace: bool | None = None,
    health_path: str = "",
    require_ready: bool = False,
) -> WorkspaceTemplate:
    """Register a custom template into the runtime registry."""
    workdir = "/home/jovyan/work" if ide_type == "jupyter" else "/home/coder/project"
    if ide_type == "service":
        workdir = "/"
    elif ide_type == "terminal":
        workdir = "/home/devuser"
    if mount_workspace is None:
        mount_workspace = ide_type != "service"
    features = ["Özel Şablon", f"Port: {default_port}", f"Görüntü: {image_tag}"]
    
    tpl = WorkspaceTemplate(
        id=template_id,
        name=name,
        description=description,
        category=category,
        icon=icon,
        default_port=default_port,
        container_workdir=workdir,
        image_tag=image_tag,
        features=features,
        ide_type=ide_type,
        env_vars=(
            {"JUPYTER_ENABLE_LAB": "yes"}
            if ide_type == "jupyter"
            else ({"DOCKER_USER": "coder"} if ide_type == "vscode" else {})
        ),
        mount_workspace=mount_workspace,
        health_path=health_path,
        require_ready=require_ready,
    )
    TEMPLATES[template_id] = tpl
    return tpl


async def resolve_template(db, template_id: str) -> WorkspaceTemplate | None:
    """Lookup template from memory or load from custom_templates database table."""
    if template_id in TEMPLATES:
        return TEMPLATES[template_id]

    from sqlalchemy import select
    from app.models.custom_template import CustomTemplate

    stmt = select(CustomTemplate).where(CustomTemplate.id == template_id)
    res = await db.execute(stmt)
    db_tpl = res.scalar_one_or_none()
    if db_tpl:
        return register_custom_template(
            template_id=db_tpl.id,
            name=db_tpl.name,
            description=db_tpl.description,
            category=db_tpl.category,
            image_tag=db_tpl.image_tag,
            default_port=db_tpl.default_port,
            ide_type=db_tpl.ide_type,
            icon=db_tpl.icon,
        )
    return None
