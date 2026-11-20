"""Who may use a workspace template: allow and deny lists per template.

A rule entry names a directory team (AD ``department``, ``User.team``), a
müdürlük (``User.organization_unit``) or an AD security group (recorded at
login in ``User.directory_groups``):

- ``t2-arge``            a team, müdürlük or AD group with that name
- ``grup:t2-arge``       only the AD group (``group:`` works too); a full
                         group DN is reduced to its CN
- ``takım:Yapay Zeka``   only the team (``team:``)
- ``müdürlük:BTGM``      only the müdürlük (``unit:``)

Names compare case- and spacing-insensitively. Deny wins: a member of any
denied entry is refused. A non-empty allow list admits only members of at
least one allowed entry; an empty one admits everyone. Admins are never
restricted.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.template_settings import TemplateSettings
from app.models.user import User, UserRole
from app.quotas import team_key

KIND_TEAM = "team"
KIND_UNIT = "unit"
KIND_GROUP = "group"
KINDS = (KIND_TEAM, KIND_UNIT, KIND_GROUP)
KIND_PREFIXES = {
    "team": KIND_TEAM,
    "takım": KIND_TEAM,
    "takim": KIND_TEAM,
    "unit": KIND_UNIT,
    "müdürlük": KIND_UNIT,
    "mudurluk": KIND_UNIT,
    "group": KIND_GROUP,
    "grup": KIND_GROUP,
}
DISPLAY_PREFIX = {KIND_TEAM: "takım", KIND_UNIT: "müdürlük", KIND_GROUP: "grup"}
MAX_ENTRIES = 100
DENIED_MESSAGE = "Bu workspace tipini kullanma yetkiniz yok. Erişim için yöneticinize başvurun."
# Rules are read on every proxied request; admin edits take effect within this.
CACHE_SECONDS = 10.0


class AccessRuleError(ValueError):
    pass


@dataclass(frozen=True)
class Entry:
    kind: str | None  # None: any kind
    name: str  # display form, spacing cleaned

    @property
    def key(self) -> str:
        return team_key(self.name)

    def display(self) -> str:
        return f"{DISPLAY_PREFIX[self.kind]}:{self.name}" if self.kind else self.name


@dataclass(frozen=True)
class AccessRule:
    allow: tuple[Entry, ...] = ()
    deny: tuple[Entry, ...] = ()

    @property
    def restricted(self) -> bool:
        return bool(self.allow or self.deny)


OPEN_RULE = AccessRule()


def group_name(value: str) -> str:
    """``CN=t2-arge,OU=Groups,DC=x`` -> ``t2-arge``; plain names unchanged."""
    value = value.strip()
    match = re.match(r"\s*cn\s*=\s*((?:\\.|[^,])+)", value, flags=re.IGNORECASE)
    if match and "," in value:
        return re.sub(r"\\(.)", r"\1", match.group(1)).strip()
    return value


def parse_entry(raw: str) -> Entry | None:
    text = " ".join(raw.split())
    if not text:
        return None
    kind = None
    prefix, colon, rest = text.partition(":")
    if colon and prefix.strip().casefold() in KIND_PREFIXES:
        kind = KIND_PREFIXES[prefix.strip().casefold()]
        text = rest.strip()
    if "=" in text and "," in text:
        # A pasted DN is always an AD group.
        kind = kind or KIND_GROUP
        text = group_name(text)
    text = " ".join(text.split())
    if not text:
        raise AccessRuleError(f"'{raw.strip()}' kuralında ad eksik.")
    if len(text) > 255:
        raise AccessRuleError(f"'{text[:40]}…' çok uzun.")
    return Entry(kind=kind, name=text)


def parse_entries(value: str | list[str] | None) -> tuple[Entry, ...]:
    """Entries from a comma/newline separated string (or a list of them)."""
    if value is None:
        return ()
    if isinstance(value, str):
        # A DN contains commas, so DNs go on their own line.
        lines = value.splitlines()
        parts: list[str] = []
        for line in lines:
            if "=" in line and "," in line:
                parts.append(line)
            else:
                parts.extend(line.split(","))
    else:
        parts = list(value)
    entries: list[Entry] = []
    seen: set[tuple[str | None, str]] = set()
    for part in parts:
        entry = parse_entry(part)
        if entry is None or (entry.kind, entry.key) in seen:
            continue
        seen.add((entry.kind, entry.key))
        entries.append(entry)
    if len(entries) > MAX_ENTRIES:
        raise AccessRuleError(f"En fazla {MAX_ENTRIES} kural girilebilir.")
    return tuple(entries)


def dump_entries(entries: tuple[Entry, ...]) -> str:
    return json.dumps([entry.display() for entry in entries], ensure_ascii=False)


def load_entries(stored: str | None) -> tuple[Entry, ...]:
    if not stored:
        return ()
    try:
        values = json.loads(stored)
    except ValueError:
        values = stored.splitlines()
    try:
        return parse_entries([str(value) for value in values])
    except AccessRuleError:
        return ()


def format_entries(entries: tuple[Entry, ...]) -> str:
    """The admin form's text: one entry per comma."""
    return ", ".join(entry.display() for entry in entries)


def rule_from_record(record: TemplateSettings | None) -> AccessRule:
    if record is None:
        return OPEN_RULE
    return AccessRule(
        allow=load_entries(record.access_allow),
        deny=load_entries(record.access_deny),
    )


def user_groups(user: User) -> list[str]:
    stored = getattr(user, "directory_groups", "") or ""
    try:
        values = json.loads(stored)
    except ValueError:
        values = stored.splitlines()
    return [str(value) for value in values if str(value).strip()]


def _memberships(user: User) -> dict[str, set[str]]:
    return {
        KIND_TEAM: {key for key in [team_key(user.team)] if key},
        KIND_UNIT: {key for key in [team_key(user.organization_unit)] if key},
        KIND_GROUP: {team_key(name) for name in user_groups(user) if team_key(name)},
    }


def _matches(entry: Entry, memberships: dict[str, set[str]]) -> bool:
    kinds = (entry.kind,) if entry.kind else KINDS
    return any(entry.key in memberships[kind] for kind in kinds)


def user_allowed(user: User, rule: AccessRule) -> bool:
    if user.role == UserRole.ADMIN or not rule.restricted:
        return True
    memberships = _memberships(user)
    if any(_matches(entry, memberships) for entry in rule.deny):
        return False
    return not rule.allow or any(_matches(entry, memberships) for entry in rule.allow)


# ---- loading ------------------------------------------------------------

_cache: tuple[float, dict[str, AccessRule]] | None = None


def invalidate_cache() -> None:
    global _cache
    _cache = None


async def load_rules(db: AsyncSession, *, cached: bool = True) -> dict[str, AccessRule]:
    global _cache
    now = time.monotonic()
    if cached and _cache and _cache[0] > now:
        return _cache[1]
    records = (await db.execute(select(TemplateSettings))).scalars().all()
    rules = {
        record.template_id: rule
        for record in records
        if (rule := rule_from_record(record)).restricted
    }
    _cache = (now + CACHE_SECONDS, rules)
    return rules


async def can_use_template(db: AsyncSession, user: User, template_id: str, *, cached: bool = True) -> bool:
    if user.role == UserRole.ADMIN:
        return True
    rules = await load_rules(db, cached=cached)
    return user_allowed(user, rules.get(template_id, OPEN_RULE))


async def require_template_access(
    db: AsyncSession, user: User, template_id: str, *, cached: bool = False
) -> None:
    """Raise 403 unless ``user`` may use ``template_id``."""
    if not await can_use_template(db, user, template_id, cached=cached):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=DENIED_MESSAGE)


async def filter_templates(db: AsyncSession, user: User | None, templates: list):
    """Templates ``user`` may use; everything when nobody is signed in."""
    if user is None or user.role == UserRole.ADMIN:
        return templates
    rules = await load_rules(db, cached=False)
    return [
        template
        for template in templates
        if user_allowed(user, rules.get(template.id, OPEN_RULE))
    ]
