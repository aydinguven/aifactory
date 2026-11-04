"""Admin API: the skill library shared with Kilo Code and Jupyter AI workspaces."""

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_admin_user
from app.database import get_db
from app.models.skill import Skill
from app.models.user import User
from app.schemas.skill import SkillLibraryOut, SkillOut, SkillSave
from app.skills import MAX_BUNDLE_LENGTH, build_bundle, parse_skill

router = APIRouter()


async def _all_skills(db: AsyncSession) -> list[Skill]:
    return list((await db.execute(select(Skill).order_by(Skill.name))).scalars().all())


def _enabled_bundle(skills: list[Skill]) -> str:
    return build_bundle([(skill.name, skill.content) for skill in skills if skill.enabled])


def _library_out(skills: list[Skill]) -> SkillLibraryOut:
    return SkillLibraryOut(
        skills=[SkillOut.model_validate(skill) for skill in skills],
        bundle_length=len(_enabled_bundle(skills)),
        bundle_limit=MAX_BUNDLE_LENGTH,
    )


async def _save(
    db: AsyncSession,
    data: SkillSave,
    skill: Skill | None,
) -> Skill:
    try:
        parsed = parse_skill(data.content)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    skills = await _all_skills(db)
    if any(other.name == parsed.name and other is not skill for other in skills):
        raise HTTPException(
            status_code=409, detail=f"'{parsed.name}' adlı bir skill zaten var."
        )
    candidate = [
        (other.name, other.content)
        for other in skills
        if other.enabled and other is not skill
    ]
    if data.enabled:
        candidate.append((parsed.name, parsed.content))
    if len(build_bundle(candidate)) > MAX_BUNDLE_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=(
                "Etkin skill'lerin sıkıştırılmış toplam boyutu workspace'lere "
                "gönderilebilecek sınırı aşıyor. Bir skill'i kısaltın veya devre "
                "dışı bırakın."
            ),
        )
    now = datetime.now(timezone.utc)
    if skill is None:
        skill = Skill(created_at=now)
    skill.name = parsed.name
    skill.description = parsed.description
    skill.content = parsed.content
    skill.enabled = data.enabled
    skill.updated_at = now
    db.add(skill)
    await db.commit()
    await db.refresh(skill)
    return skill


@router.get("/skills", response_model=SkillLibraryOut)
async def list_skills(
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    return _library_out(await _all_skills(db))


@router.post("/skills", response_model=SkillOut, status_code=status.HTTP_201_CREATED)
async def create_skill(
    data: SkillSave,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Add a SKILL.md; it reaches workspaces created or restarted afterwards."""
    return await _save(db, data, None)


@router.put("/skills/{skill_id}", response_model=SkillOut)
async def update_skill(
    skill_id: int,
    data: SkillSave,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill bulunamadı.")
    return await _save(db, data, skill)


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: int,
    _admin: Annotated[User, Depends(get_current_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    skill = await db.get(Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill bulunamadı.")
    await db.delete(skill)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
