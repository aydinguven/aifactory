"""LLM usage statistics from LiteLLM for the admin panel and the GenAI stats page.

Two admin calls cover everything: ``/user/daily/activity`` without a user
filter (daily totals, per-user and per-model breakdown) and
``/team/daily/activity`` (per-team breakdown with aliases). Both paginate over
raw spend rows, so pages are merged by date. Results are cached briefly so
page loads do not hammer LiteLLM.
"""

import asyncio
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.genai import GenAiUnavailable, _client, litellm_user_id
from app.integrations.litellm import LiteLLMClient, LiteLLMConnectionError
from app.models.genai_account import GenAiAccount
from app.models.user import User
from app.quotas import DEFAULT_GROUP_KEY, UNIT_KEY_PREFIX, clean_name, team_key, unit_key

METRICS = (
    "spend",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "api_requests",
    "successful_requests",
    "failed_requests",
)
CACHE_SECONDS = 60
MAX_PAGES = 50
# LiteLLM books requests that carry no user under this id. It is not a person,
# so it is left out of the user, team and müdürlük breakdowns.
LITELLM_DEFAULT_USER_ID = "default_user_id"

_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = asyncio.Lock()


def _empty() -> dict:
    return {name: 0 for name in METRICS} | {"spend": 0.0}


def _add(target: dict, metrics: dict | None) -> None:
    for name in METRICS:
        value = (metrics or {}).get(name) or 0
        try:
            target[name] += float(value) if name == "spend" else int(value)
        except (TypeError, ValueError):
            continue


def _rounded(metrics: dict) -> dict:
    return metrics | {"spend": round(float(metrics.get("spend") or 0), 6)}


async def _activity(client: LiteLLMClient, path: str, start: date, end: date) -> list[dict]:
    rows: list[dict] = []
    for page in range(1, MAX_PAGES + 1):
        payload = await client._request(
            "GET",
            path,
            params={
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "page": page,
                "page_size": 1000,
            },
        )
        rows.extend(item for item in payload.get("results") or [] if isinstance(item, dict))
        if not (payload.get("metadata") or {}).get("has_more"):
            break
    return rows


def _merge(rows: list[dict]) -> dict:
    daily: dict[str, dict] = {}
    entities: dict[str, dict] = {}
    entity_daily: dict[str, dict[str, dict]] = {}
    entity_meta: dict[str, dict] = {}
    models: dict[str, dict] = {}
    for row in rows:
        day = str(row.get("date") or "")[:10]
        if not day:
            continue
        _add(daily.setdefault(day, _empty()), row.get("metrics"))
        breakdown = row.get("breakdown") or {}
        for entity, item in (breakdown.get("entities") or {}).items():
            if not isinstance(item, dict):
                continue
            _add(entities.setdefault(entity, _empty()), item.get("metrics"))
            _add(entity_daily.setdefault(entity, {}).setdefault(day, _empty()), item.get("metrics"))
            if isinstance(item.get("metadata"), dict) and item["metadata"]:
                entity_meta[entity] = item["metadata"]
        for model, item in (breakdown.get("models") or {}).items():
            if isinstance(item, dict):
                _add(models.setdefault(model, _empty()), item.get("metrics"))
    return {
        "daily": daily,
        "entities": entities,
        "entity_daily": entity_daily,
        "entity_meta": entity_meta,
        "models": models,
    }


async def _fetch(client: LiteLLMClient, start: date, end: date) -> dict:
    users = _merge(await _activity(client, "/user/daily/activity", start, end))
    try:
        teams = _merge(await _activity(client, "/team/daily/activity", start, end))
    except LiteLLMConnectionError:
        # Older LiteLLM releases have no team analytics; users still work.
        teams = None
    return {"users": users, "teams": teams}


async def usage_stats(db: AsyncSession, days: int = 30, viewer: User | None = None) -> dict:
    """Aggregated usage for the last ``days`` days, mapped to AI Factory users.

    With a ``viewer``, ``my_team`` holds their team's usage and members, and
    ``my_unit`` (unit heads only) their müdürlük with each of its teams.
    """
    days = max(1, min(int(days), 90))
    end = date.today()
    start = end - timedelta(days=days - 1)
    try:
        client, _record = await _client(db)
    except GenAiUnavailable as exc:
        return {"available": False, "error": str(exc), "days": days}
    key = (client.config.base_url, start, end)
    async with _cache_lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            raw = cached[1]
        else:
            try:
                raw = await _fetch(client, start, end)
            except LiteLLMConnectionError as exc:
                return {"available": False, "error": str(exc), "days": days}
            _cache.clear()
            _cache[key] = (time.monotonic(), raw)
    return await _shape(db, raw, start, end, days, viewer)


@dataclass
class _Org:
    """Team and müdürlük structure of every AI Factory user, active or not."""

    team_names: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    team_units: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    team_sizes: Counter = field(default_factory=Counter)
    unit_names: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    unit_sizes: Counter = field(default_factory=Counter)

    def add(self, user: User) -> None:
        tkey = team_key(user.team)
        unit = clean_name(user.organization_unit)
        if tkey:
            self.team_names[tkey][clean_name(user.team)] += 1
            self.team_sizes[tkey] += 1
            if unit:
                self.team_units[tkey][unit] += 1
        if unit:
            self.unit_names[unit_key(unit)][unit] += 1
            self.unit_sizes[unit_key(unit)] += 1

    @staticmethod
    def _top(counter: Counter | None) -> str:
        if not counter:
            return ""
        return max(counter.items(), key=lambda item: (item[1], item[0]))[0]

    def team_name(self, tkey: str) -> str:
        return self._top(self.team_names.get(tkey)) or tkey

    def team_unit(self, tkey: str) -> str:
        """The müdürlük most members of the team belong to."""
        return self._top(self.team_units.get(tkey))

    def unit_name(self, ukey: str) -> str:
        return self._top(self.unit_names.get(ukey)) or ukey.removeprefix(UNIT_KEY_PREFIX)

    def unit_teams(self, ukey: str) -> list[str]:
        return [tkey for tkey in self.team_units if unit_key(self.team_unit(tkey)) == ukey]


async def _directory(db: AsyncSession) -> tuple[dict[str, dict], _Org]:
    """LiteLLM user id -> AI Factory identity, and the organization structure."""
    accounts = {
        row.user_id: row.litellm_user_id
        for row in (await db.execute(select(GenAiAccount))).scalars()
    }
    people: dict[str, dict] = {}
    org = _Org()
    for user in (await db.execute(select(User))).scalars():
        org.add(user)
        try:
            litellm_id = accounts.get(user.id) or litellm_user_id(user)
        except Exception:  # noqa: BLE001 - usernames LiteLLM cannot represent
            continue
        people[litellm_id] = {
            "user_id": user.id,
            "username": user.username,
            "full_name": user.full_name or "",
            "team": clean_name(user.team),
            "team_key": team_key(user.team),
            "unit": clean_name(user.organization_unit),
            "unit_key": unit_key(user.organization_unit),
        }
    return people, org


def _bucket(key: str, name: str, size: int) -> dict:
    return {
        "key": key,
        "name": name,
        "members": 0,
        **_empty(),
        "daily_tokens": [0] * size,
        "daily_requests": [0] * size,
    }


def _accumulate(target: dict, metrics: dict, series: dict) -> None:
    target["members"] += 1
    _add(target, metrics)
    for index, tokens in enumerate(series["daily_tokens"]):
        target["daily_tokens"][index] += tokens
        target["daily_requests"][index] += series["daily_requests"][index]


def _add_days(target: dict[str, dict], per_day: dict[str, dict]) -> None:
    for day, metrics in per_day.items():
        _add(target.setdefault(day, _empty()), metrics)


async def _shape(
    db: AsyncSession,
    raw: dict,
    start: date,
    end: date,
    days: int,
    viewer: User | None = None,
) -> dict:
    users_raw = raw["users"]
    teams_raw = raw["teams"]
    people, org = await _directory(db)
    dates = [(start + timedelta(days=offset)).isoformat() for offset in range(days)]

    def daily(per_day: dict[str, dict]) -> list[dict]:
        return [{"date": day, **_rounded(per_day.get(day, _empty()))} for day in dates]

    totals = _empty()
    for metrics in users_raw["daily"].values():
        _add(totals, metrics)

    users = []
    groups: dict[str, dict] = {}
    units: dict[str, dict] = {}
    group_days: dict[str, dict[str, dict]] = defaultdict(dict)
    unit_days: dict[str, dict[str, dict]] = defaultdict(dict)
    for entity, metrics in users_raw["entities"].items():
        if entity == LITELLM_DEFAULT_USER_ID:
            continue  # pre-LDAP traffic without a user; stays in the totals only
        person = people.get(entity)
        per_day = users_raw["entity_daily"].get(entity, {})
        series = {
            "daily_tokens": [per_day.get(day, {}).get("total_tokens", 0) for day in dates],
            "daily_requests": [per_day.get(day, {}).get("api_requests", 0) for day in dates],
        }
        group_key = person["team_key"] if person else DEFAULT_GROUP_KEY
        member_unit_key = person["unit_key"] if person else ""
        users.append(
            {
                "litellm_user_id": entity,
                "username": person["username"] if person else entity,
                "full_name": person["full_name"] if person else "",
                "team": person["team"] if person else "",
                "team_key": group_key,
                "unit": person["unit"] if person else "",
                "unit_key": member_unit_key,
                "devcloud_user_id": person["user_id"] if person else None,
                **_rounded(metrics),
                **series,
            }
        )
        if group_key not in groups:
            name = (person["team"] if person else "") or "Takımsız"
            groups[group_key] = _bucket(group_key, name, len(dates))
        _accumulate(groups[group_key], metrics, series)
        _add_days(group_days[group_key], per_day)
        if member_unit_key:
            if member_unit_key not in units:
                units[member_unit_key] = _bucket(
                    member_unit_key, org.unit_name(member_unit_key), len(dates)
                )
            _accumulate(units[member_unit_key], metrics, series)
            _add_days(unit_days[member_unit_key], per_day)
    users.sort(key=lambda item: item["total_tokens"], reverse=True)

    for group in groups.values():
        unit = org.team_unit(group["key"]) if group["key"] else ""
        group.update(
            unit=unit,
            unit_key=unit_key(unit),
            member_total=org.team_sizes.get(group["key"], 0),
        )
    for unit in units.values():
        unit.update(
            team_count=len(org.unit_teams(unit["key"])),
            member_total=org.unit_sizes.get(unit["key"], 0),
        )

    def team_row(tkey: str) -> dict:
        """A team's usage row, or a zero row for a team without usage."""
        if tkey in groups:
            return _rounded(groups[tkey])
        unit = org.team_unit(tkey)
        return _rounded(
            _bucket(tkey, org.team_name(tkey), len(dates))
            | {"unit": unit, "unit_key": unit_key(unit), "member_total": org.team_sizes.get(tkey, 0)}
        )

    my_team = None
    if viewer is not None and team_key(viewer.team):
        tkey = team_key(viewer.team)
        my_team = team_row(tkey) | {
            "daily": daily(group_days.get(tkey, {})),
            "users": [row for row in users if row["team_key"] == tkey],
        }

    my_unit = None
    if viewer is not None and unit_key(viewer.managed_unit):
        ukey = unit_key(viewer.managed_unit)
        row = units.get(ukey) or _bucket(ukey, clean_name(viewer.managed_unit), len(dates)) | {
            "team_count": len(org.unit_teams(ukey)),
            "member_total": org.unit_sizes.get(ukey, 0),
        }
        unit_teams = sorted(
            (team_row(tkey) for tkey in org.unit_teams(ukey)),
            key=lambda item: item["total_tokens"],
            reverse=True,
        )
        my_unit = _rounded(row) | {
            "daily": daily(unit_days.get(ukey, {})),
            "teams": unit_teams,
            "team_series": [
                {"name": team["name"], "values": daily(group_days.get(team["key"], {}))}
                for team in unit_teams
            ],
            "users": [row for row in users if row["unit_key"] == ukey],
        }

    teams = []
    team_series = []
    if teams_raw is not None:
        for entity, metrics in teams_raw["entities"].items():
            alias = str((teams_raw["entity_meta"].get(entity) or {}).get("team_alias") or "")
            name = alias or ("Takımsız anahtarlar" if entity == "Unassigned" else entity)
            teams.append({"team_id": entity, "name": name, **_rounded(metrics)})
            per_day = teams_raw["entity_daily"].get(entity, {})
            team_series.append(
                {
                    "name": name,
                    "values": [_rounded(per_day.get(day, _empty())) for day in dates],
                }
            )
        teams.sort(key=lambda item: item["total_tokens"], reverse=True)
        order = [team["name"] for team in teams]
        team_series.sort(key=lambda series: order.index(series["name"]))

    models = [
        {"model": model, **_rounded(metrics)}
        for model, metrics in users_raw["models"].items()
    ]
    models.sort(key=lambda item: item["total_tokens"], reverse=True)

    return {
        "available": True,
        "days": days,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "totals": _rounded(totals) | {"active_users": len(users)},
        "daily": [
            {"date": day, **_rounded(users_raw["daily"].get(day, _empty()))} for day in dates
        ],
        "users": users,
        "user_breakdown": bool(users) or not totals["api_requests"],
        "groups": sorted(
            (_rounded(group) for group in groups.values()),
            key=lambda item: item["total_tokens"],
            reverse=True,
        ),
        "units": sorted(
            (_rounded(unit) for unit in units.values()),
            key=lambda item: item["total_tokens"],
            reverse=True,
        ),
        "viewer": {
            "team_key": team_key(viewer.team) if viewer else "",
            "unit_key": unit_key(viewer.organization_unit) if viewer else "",
            "managed_unit_key": unit_key(viewer.managed_unit) if viewer else "",
            "user_id": viewer.id if viewer else None,
        },
        "my_team": my_team,
        "my_unit": my_unit,
        "teams": teams,
        "team_breakdown": teams_raw is not None,
        "team_series": team_series,
        "models": models,
    }
