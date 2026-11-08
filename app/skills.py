"""Admin skill library: SKILL.md files delivered to Kilo Code and Jupyter AI.

Each skill is one Agent Skills ``SKILL.md`` (YAML frontmatter with ``name``
and ``description``, Markdown body). The enabled skills reach a workspace as
one gzip tar archive in base64 (``ai_skills_bundle`` in container.create);
the container's startup command unpacks it into a user-level skills folder:

- Kilo Code: ``SKILLS_DIR``, listed in the global ``kilo.jsonc`` ``skills``.
- Jupyter AI (Claude Code via ACP): ``$CLAUDE_CONFIG_DIR/skills``.

Both agents only read each skill's name and description up front and load
the body when a request matches, so a library of many skills stays cheap.
"""

from __future__ import annotations

import base64
import binascii
import gzip
import io
import re
import tarfile
import zlib
from dataclasses import dataclass

import yaml

# First worker release that accepts ai_skills_bundle; older workers drop it.
MIN_AGENT_VERSION = "3.27.0"
# Kilo Code's copy, outside the folders Kilo scans by default (~/.kilo,
# ~/.agents, ~/.claude, ~/.config/kilo) so no skill is discovered twice.
SKILLS_DIR = "/home/coder/.aifactory/skills"
SKILL_FILENAME = "SKILL.md"
NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_LENGTH = 65536
# The bundle is one ``podman run -e`` argument; Linux caps a single argument
# at 128 KiB (MAX_ARG_STRLEN), and the key and some headroom need room too.
MAX_BUNDLE_LENGTH = 120_000
# Unpacked tar size the worker accepts (guards against a gzip bomb).
MAX_UNPACKED_LENGTH = 8 * 1024 * 1024
_FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*(?:\n|\Z)", re.DOTALL)


@dataclass(frozen=True)
class ParsedSkill:
    name: str
    description: str
    content: str


def normalize_content(content: str) -> str:
    text = str(content or "").lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    text = text.strip("\n")
    return f"{text}\n" if text else ""


def parse_skill(content: str) -> ParsedSkill:
    """Validate a SKILL.md and return its name and description.

    Raises ValueError with a message for the Admin.
    """
    text = normalize_content(content)
    if not text:
        raise ValueError("SKILL.md içeriği boş.")
    if len(text) > MAX_SKILL_LENGTH:
        raise ValueError(
            f"SKILL.md en fazla {MAX_SKILL_LENGTH // 1024} KB olabilir."
        )
    match = _FRONTMATTER.match(text)
    if not match:
        raise ValueError(
            "SKILL.md '---' satırları arasında YAML frontmatter ile başlamalı."
        )
    try:
        metadata = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(f"Frontmatter geçerli YAML değil: {exc}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("Frontmatter 'name' ve 'description' alanları içermeli.")
    name = metadata.get("name")
    description = metadata.get("description")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Frontmatter 'name' alanı zorunlu.")
    name = name.strip()
    if len(name) > MAX_NAME_LENGTH or not NAME_PATTERN.fullmatch(name):
        raise ValueError(
            "Skill adı en fazla 64 karakter olmalı; yalnızca küçük harf, rakam ve "
            "tire içerebilir, tire ile başlayıp bitemez."
        )
    if not isinstance(description, str) or not description.strip():
        raise ValueError("Frontmatter 'description' alanı zorunlu.")
    description = description.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise ValueError("Skill açıklaması en fazla 1024 karakter olabilir.")
    if not text[match.end():].strip():
        raise ValueError("SKILL.md frontmatter'dan sonra talimat içermeli.")
    return ParsedSkill(name=name, description=description, content=text)


def build_bundle(skills: list[tuple[str, str]]) -> str:
    """Pack ``(name, content)`` pairs as a deterministic base64 gzip tar.

    The same skills always give the same string, so the workspace AI config
    digest only changes when a skill does.
    """
    if not skills:
        return ""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, content in sorted(skills):
            folder = tarfile.TarInfo(name)
            folder.type = tarfile.DIRTYPE
            folder.mode = 0o755
            archive.addfile(folder)
            data = normalize_content(content).encode("utf-8")
            entry = tarfile.TarInfo(f"{name}/{SKILL_FILENAME}")
            entry.size = len(data)
            entry.mode = 0o644
            archive.addfile(entry, io.BytesIO(data))
    compressed = gzip.compress(raw.getvalue(), compresslevel=9, mtime=0)
    return base64.b64encode(compressed).decode("ascii")


def validated_bundle(raw: object) -> str:
    """Return a bundle only if it is bounded and holds just ``<name>/SKILL.md`` files.

    Run by the worker before the bundle reaches ``tar`` in a container.
    """
    if not isinstance(raw, str) or not raw or len(raw) > MAX_BUNDLE_LENGTH:
        return ""
    try:
        decompressor = zlib.decompressobj(wbits=31)  # gzip container
        data = decompressor.decompress(
            base64.b64decode(raw, validate=True), MAX_UNPACKED_LENGTH + 1
        )
        # tar -z in the container also reads any further gzip member, so
        # anything after the first one would bypass this check.
        if (
            len(data) > MAX_UNPACKED_LENGTH
            or not decompressor.eof
            or decompressor.unused_data
        ):
            return ""
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
            members = archive.getmembers()
    except (binascii.Error, OSError, EOFError, tarfile.TarError, ValueError, zlib.error):
        return ""
    if not members:
        return ""
    for member in members:
        name, _, filename = member.name.partition("/")
        if len(name) > MAX_NAME_LENGTH or not NAME_PATTERN.fullmatch(name):
            return ""
        if member.isdir() and not filename:
            continue
        if not member.isreg() or filename != SKILL_FILENAME:
            return ""
    return raw
