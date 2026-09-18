from __future__ import annotations

from typing import Iterator, Literal

from pydantic import BaseModel, Field


class DocMeta(BaseModel):
    source_format: Literal["docx", "pptx", "pdf"]
    source_name: str
    n_chars: int = 0
    parse_warnings: list[str] = Field(default_factory=list)


class DocTable(BaseModel):
    """源表格，单元格值 verbatim，后续永不经 LLM。"""

    table_id: str
    section_id: str
    n_rows: int
    n_cols: int
    header: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    caption: str | None = None

    def flat_text(self) -> str:
        parts = [self.caption] if self.caption else []
        parts.extend(self.header)
        for row in self.rows:
            parts.extend(row)
        return " ".join(p for p in parts if p)


class DocBlock(BaseModel):
    block_id: str
    kind: Literal["para", "list_item", "table"]
    text: str = ""
    table_id: str | None = None


class DocSection(BaseModel):
    section_id: str
    level: int
    title: str
    blocks: list[DocBlock] = Field(default_factory=list)
    subsections: list["DocSection"] = Field(default_factory=list)

    def walk(self) -> Iterator["DocSection"]:
        yield self
        for sub in self.subsections:
            yield from sub.walk()

    def text_of(self) -> str:
        return " ".join(b.text for b in self.blocks if b.text)


class DocTree(BaseModel):
    """解析器唯一输出：归一化文档树。full_text 是数字溯源与 n-gram 的匹配基准。"""

    meta: DocMeta
    sections: list[DocSection]
    tables: list[DocTable]
    full_text: str

    def _index(self) -> tuple[dict[str, DocSection], dict[str, DocBlock], dict[str, DocTable]]:
        sections: dict[str, DocSection] = {}
        blocks: dict[str, DocBlock] = {}
        for sec in self.sections:
            for s in sec.walk():
                sections[s.section_id] = s
                for b in s.blocks:
                    blocks[b.block_id] = b
        tables = {t.table_id: t for t in self.tables}
        return sections, blocks, tables

    def resolve(self, refs: list[str], per_section_limit: int = 2000) -> str:
        """src_ref（section_id / block_id / table_id）→ 原文片段，喂给 fill。"""
        sections, blocks, tables = self._index()
        out: list[str] = []
        for ref in refs:
            if ref in sections:
                sec = sections[ref]
                text = f"{sec.title}。{sec.text_of()}"
                out.append(text[:per_section_limit])
            elif ref in blocks:
                b = blocks[ref]
                if b.kind == "table" and b.table_id and b.table_id in tables:
                    out.append(tables[b.table_id].flat_text())
                elif b.text:
                    out.append(b.text)
            elif ref in tables:
                out.append(tables[ref].flat_text())
        return "\n".join(out)

    def known_refs(self) -> set[str]:
        sections, blocks, tables = self._index()
        return set(sections) | set(blocks) | set(tables)
