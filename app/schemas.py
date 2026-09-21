"""Pydantic request models."""
from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

_DT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


def _check_dt(value: str) -> str:
    if not _DT_RE.match(value or ""):
        raise ValueError("时间格式应为 YYYY-MM-DDTHH:MM")
    return value


class MeetingCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    start_time: str
    end_time: str

    @field_validator("start_time", "end_time")
    @classmethod
    def _v_dt(cls, v: str) -> str:
        return _check_dt(v)


class ItemCreate(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    planned_minutes: int = Field(gt=0, le=600)
    minimum_minutes: int = Field(ge=0, le=600)
    priority: int = Field(default=3, ge=1, le=5)
    incompressible: bool = False


class ActualIn(BaseModel):
    item_id: int
    actual_end: str
    # optimistic-concurrency token read from the latest plan/preview
    revision: int = Field(ge=0)

    @field_validator("actual_end")
    @classmethod
    def _v_dt(cls, v: str) -> str:
        return _check_dt(v)


class MoveIn(BaseModel):
    direction: str = Field(pattern="^(up|down)$")


class RevisionIn(BaseModel):
    """Body for live-state mutations guarded by optimistic concurrency."""
    revision: int = Field(ge=0)


class PostponeIn(BaseModel):
    item_ids: list[int] = Field(min_length=1, max_length=500)
    # must match the revision returned together with the confirmed preview
    revision: int = Field(ge=0)


class BreakCreate(BaseModel):
    title: str = Field(default="休息", max_length=100)
    fixed_start: str
    duration_minutes: int = Field(gt=0, le=300)

    @field_validator("fixed_start")
    @classmethod
    def _v_dt(cls, v: str) -> str:
        return _check_dt(v)
