from __future__ import annotations

from pydantic import BaseModel, Field

from app.schema.enums import TableStrategy


class CellPos(BaseModel):
    row: int
    col: int


class KeyFact(BaseModel):
    """text 必须为原文逐字摘录，由 verify_facts() 代码回验。"""

    text: str
    block_ids: list[str] = Field(default_factory=list)


class TableDigest(BaseModel):
    table_id: str
    topic: str
    headline_cells: list[CellPos] = Field(default_factory=list)
    suggested_strategy: TableStrategy
    rationale: str = ""


class SectionMap(BaseModel):
    section_id: str
    summary: str
    key_facts: list[KeyFact] = Field(default_factory=list)
    table_digests: list[TableDigest] = Field(default_factory=list)


class DocMap(BaseModel):
    doc_title: str
    section_maps: list[SectionMap]
