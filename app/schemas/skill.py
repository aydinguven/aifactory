from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.skills import MAX_SKILL_LENGTH


class SkillSave(BaseModel):
    # Whole SKILL.md; name and description come from its frontmatter.
    # The length limit here is generous: parse_skill applies the real one
    # after newline normalization.
    content: str = Field(max_length=MAX_SKILL_LENGTH * 2)
    enabled: bool = True


class SkillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    content: str
    enabled: bool
    created_at: datetime
    updated_at: datetime


class SkillLibraryOut(BaseModel):
    skills: list[SkillOut]
    # Size of the enabled skills' bundle and its limit, in characters.
    bundle_length: int
    bundle_limit: int
