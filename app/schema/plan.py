from __future__ import annotations

from pydantic import BaseModel, Field

from app.schema.enums import SlideType, TableStrategy


class TableRef(BaseModel):
    table_id: str
    strategy: TableStrategy


class SlidePlanItem(BaseModel):
    page_no: int
    slide_type: SlideType
    title: str
    brief: str = ""
    src_refs: list[str] = Field(default_factory=list)
    table_ref: TableRef | None = None


class SlidePlan(BaseModel):
    title: str
    pages: list[SlidePlanItem]
