import importlib.util
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]


def test_jupyter_workspace_image_installs_jupyter_ai_and_claude_acp():
    containerfile = (ROOT / "containers/jupyter-python/Containerfile").read_text(
        encoding="utf-8"
    )

    assert '"jupyter-ai[magics]==3.1.3"' in containerfile
    assert "@anthropic-ai/claude-code@2.1.251" in containerfile
    assert "@agentclientprotocol/claude-agent-acp@0.70.0" in containerfile
    assert "nodejs=22" in containerfile
    assert "python /tmp/rename_claude_persona.py /tmp/tcmb_emblem.svg" in containerfile
    assert "COPY tcmb_emblem.svg /tmp/tcmb_emblem.svg" in containerfile


def test_claude_persona_is_branded_without_changing_its_implementation(tmp_path):
    script_path = (
        ROOT / "containers/jupyter-python/rename_claude_persona.py"
    )
    spec = importlib.util.spec_from_file_location(
        "rename_claude_persona",
        script_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    persona_path = tmp_path / "jupyter_ai_acp_client/acp_personas/claude.py"
    persona_path.parent.mkdir(parents=True)
    avatar_source_path = tmp_path / "source-tcmb-emblem.svg"
    avatar_source_path.write_text("<svg>TCMB</svg>\n", encoding="utf-8")
    original = '''class ClaudeAcpPersona(BaseAcpPersona):
    @property
    def defaults(self):
        return PersonaDefaults(
            name="Claude",
            description="Claude Code as an ACP agent persona.",
            avatar_path=os.path.join("static", "claude.svg"),
            system_prompt="unused",
        )
'''
    persona_path.write_text(original, encoding="utf-8")

    module.personalize_persona(persona_path, avatar_source_path)
    updated = persona_path.read_text(encoding="utf-8")
    avatar_target_path = tmp_path / "jupyter_ai_acp_client/static/tcmb_emblem.svg"

    assert 'name="TCMB Asistan"' in updated
    assert '"tcmb_emblem.svg"' in updated
    assert avatar_target_path.read_text(encoding="utf-8") == "<svg>TCMB</svg>\n"
    assert (
        updated
        .replace('name="TCMB Asistan"', 'name="Claude"')
        .replace('"tcmb_emblem.svg"', '"claude.svg"')
        == original
    )


def test_claude_persona_is_located_without_importing_adapter(tmp_path, monkeypatch):
    script_path = (
        ROOT / "containers/jupyter-python/rename_claude_persona.py"
    )
    spec = importlib.util.spec_from_file_location(
        "rename_claude_persona_path",
        script_path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    persona_path = tmp_path / module.PERSONA_MODULE
    persona_path.parent.mkdir(parents=True)
    persona_path.write_text('name="Claude",\n', encoding="utf-8")
    fake_distribution = SimpleNamespace(
        locate_file=lambda relative_path: tmp_path / relative_path
    )
    monkeypatch.setattr(module, "distribution", lambda name: fake_distribution)

    assert module.installed_persona_path() == persona_path.resolve()


def test_worker_forwards_shared_gateway_to_all_workspace_ai_clients():
    podman_service = (ROOT / "app/orchestrator/podman_service.py").read_text(
        encoding="utf-8"
    )

    vscode_branch, jupyter_branch = podman_service.split(
        "elif is_jupyter:", 1
    )
    vscode_branch = vscode_branch.split("if is_vscode:", 1)[1]
    jupyter_branch = jupyter_branch.split(
        "for k, v in template.env_vars.items():", 1
    )[0]
    assert "managed_cline_files" in vscode_branch
    assert "AIFACTORY_CLINE_SECRETS_JSON=" in vscode_branch
    assert "AIFACTORY_CLINE_PROVIDERS_JSON=" in vscode_branch
    assert "AIFACTORY_VSCODE_CHAT_MODELS_JSON=" not in vscode_branch
    assert "AIFACTORY_VSCODE_SETTINGS_JSON=" in vscode_branch
    assert "chatLanguageModels.json" not in vscode_branch
    assert "--uninstall-extension saoudrizwan.claude-dev" not in podman_service
    assert "--disable-extension saoudrizwan.claude-dev" not in podman_service
    assert "ANTHROPIC_AUTH_TOKEN" in jupyter_branch
    assert "ANTHROPIC_BASE_URL" in jupyter_branch
    assert "CLAUDE_CODE_EXECUTABLE=/opt/conda/bin/claude" in jupyter_branch
    assert "AIFACTORY_CLAUDE_SETTINGS_JSON=" in jupyter_branch
    assert "CLAUDE_CODE_DISABLE_1M_CONTEXT=1" in jupyter_branch
