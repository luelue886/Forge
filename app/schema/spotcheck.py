"""视觉抽检 schema：渲染页排版判定与修改意见。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SpotArea = Literal["table", "title", "para", "layout"]


class SpotIssue(BaseModel):
    area: SpotArea
    severity: Literal["major", "minor"]
    problem: str
    suggestion: str = ""


class SpotCheckOut(BaseModel):
    passed: bool
    issues: list[SpotIssue] = Field(default_factory=list)
