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
    image_ids: list[str] = Field(default_factory=list)  # 原样复用图片，节末表格之后


class DocPlan(BaseModel):
    schema_version: Literal["docplan/1.0"] = SCHEMA_VERSION
    genre: Genre
    title: str  # 文档标题：源标题照抄，不经 LLM（≤30 汉字当量由校验把关）
    items: list[DocPlanItem] = Field(default_factory=list)
