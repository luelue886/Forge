"""槽填充产物（PageFill/DeckBuild）与真模板构建（template_build）的输入契约。

替代 slideir 的 PPT 线数据流：每页不再手绘文本框，而是对模板页的槽位
（slot_id = shape 树索引路径）做填充/删除。TableIR 与 ValidationIssue 自
slideir 迁入此处（slideir 将随旧渲染链移除，其 import 暂经 re-export 兼容）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schema.enums import IssueCode, SlideType
from app.schema.tplcatalog import TemplateCatalog

SCHEMA_VERSION = "slotfill/1.0"


class ValidationIssue(BaseModel):
    rule: IssueCode
    page_no: int
    detail: str


class TableIR(BaseModel):
    table_id: str  # 溯源锚点
    header: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)  # 全 verbatim


class PageFill(BaseModel):
    schema_version: Literal["slotfill/1.0"] = SCHEMA_VERSION
    page_no: int
    slide_type: SlideType
    template_slide: int  # 目标模板页号（sldIdLst 顺序，1 起）
    title: str  # 计划标题回显（确认页展示）
    slot_values: dict[str, str] = Field(default_factory=dict)  # slot_id → 文本，"\n" = 新段落
    slot_deletes: list[str] = Field(default_factory=list)
    table: TableIR | None = None  # 仅 TABLE 页
    note: str | None = None  # 演讲备注
    src_refs: list[str] = Field(default_factory=list)


class BuildMeta(BaseModel):
    title: str
    template: str = ""  # 模板 stem
    language: str = "zh-CN"


class DeckBuild(BaseModel):
    schema_version: Literal["slotfill/1.0"] = SCHEMA_VERSION
    meta: BuildMeta
    pages: list[PageFill] = Field(default_factory=list)


def validate_deck_build(deck: DeckBuild,
                        catalog: TemplateCatalog | None = None) -> list[ValidationIssue]:
    """组装级校验：页码连续、template_slide 在界。槽级校验在 slotfill 阶段逐页做。"""
    issues: list[ValidationIssue] = []
    nums = [p.page_no for p in deck.pages]
    if nums != list(range(1, len(nums) + 1)):
        if len(nums) != len(set(nums)):
            issues.append(ValidationIssue(
                rule=IssueCode.V_PAGE_NO, page_no=0, detail=f"page_no 重复：{nums}"))
        if nums and sorted(nums) != list(range(1, len(nums) + 1)):
            issues.append(ValidationIssue(
                rule=IssueCode.V_PAGE_NO, page_no=0, detail=f"page_no 不连续：{nums}"))
    if catalog is not None:
        for p in deck.pages:
            if not 1 <= p.template_slide <= catalog.n_slides:
                issues.append(ValidationIssue(
                    rule=IssueCode.V_TEMPLATE_SLIDE, page_no=p.page_no,
                    detail=(f"template_slide {p.template_slide} 超出模板 "
                            f"{catalog.stem} 的 1-{catalog.n_slides}")))
    return issues
