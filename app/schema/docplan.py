from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schema.enums import Genre

SCHEMA_VERSION = "docplan/1.0"


class DocPlanItem(BaseModel):
    seq: int  # 从 1 起，文档顺序
    section_id: str  # 源结构锚点（DocTree 地址）
    heading: str  # 标题草稿（fill 可改写）
    heading_level: int = 1  # 1|2
    src_refs: list[str] = Field(default_factory=list)
    table_ids: list[str] = Field(default_factory=list)  # 照搬表格，渲染在该节末尾


class DocPlan(BaseModel):
    schema_version: Literal["docplan/1.0"] = SCHEMA_VERSION
    genre: Genre
    title: str  # 文档标题草稿（fill 可改写，≤30 汉字当量）
    items: list[DocPlanItem] = Field(default_factory=list)
