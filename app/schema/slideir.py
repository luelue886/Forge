from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.schema.enums import IssueCode, SlideType
from app.schema.textlen import text_weight

SCHEMA_VERSION = "slideir/1.0"


class ValidationIssue(BaseModel):
    rule: IssueCode
    page_no: int
    detail: str


class Metric(BaseModel):
    label: str
    value: str  # 恒为源文档 verbatim
    unit: str | None = None


class TableIR(BaseModel):
    table_id: str  # 溯源锚点
    header: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)  # 全 verbatim


class Column(BaseModel):
    heading: str
    bullets: list[str] = Field(default_factory=list)


class SlideIR(BaseModel):
    schema_version: Literal["slideir/1.0"] = SCHEMA_VERSION
    page_no: int
    slide_type: SlideType
    title: str
    subtitle: str | None = None
    bullets: list[str] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    table: TableIR | None = None
    columns: list[Column] = Field(default_factory=list)  # 仅 two_column，恒 2 个
    note: str | None = None  # 演讲备注（含降级表格原文）
    src_refs: list[str] = Field(default_factory=list)


class DeckMeta(BaseModel):
    title: str
    template: str = "business_blue"
    language: str = "zh-CN"


class Deck(BaseModel):
    schema_version: Literal["slideir/1.0"] = SCHEMA_VERSION
    meta: DeckMeta
    slides: list[SlideIR]


BLOCK_WHITELIST: dict[SlideType, frozenset[str]] = {
    SlideType.COVER: frozenset({"title", "subtitle"}),
    SlideType.TOC: frozenset({"title", "bullets"}),
    SlideType.SECTION_HEADER: frozenset({"title", "subtitle"}),
    SlideType.TEXT_POINTS: frozenset({"title", "bullets", "metrics", "note"}),
    SlideType.TWO_COLUMN: frozenset({"title", "columns", "note"}),
    SlideType.TABLE: frozenset({"title", "table", "note"}),
    SlideType.KEY_METRICS: frozenset({"title", "metrics", "bullets", "note"}),
    SlideType.CLOSING: frozenset({"title", "subtitle"}),
}

LIMITS = {
    "title_weight": 20.0,
    "bullet_count": 5,
    "bullet_weight": 40.0,
    "metrics_min": 2,
    "metrics_max": 4,
    "table_rows": 5,   # 含表头行
    "table_cols": 6,
    "page_budget": 200.0,  # 汉字当量；toc/cover/section_header/closing 豁免
}

_BUDGET_EXEMPT = {
    SlideType.COVER,
    SlideType.TOC,
    SlideType.SECTION_HEADER,
    SlideType.CLOSING,
}


def _present(ir: SlideIR) -> set[str]:
    fields: set[str] = set()
    if ir.title.strip():
        fields.add("title")
    if ir.subtitle is not None and ir.subtitle.strip():
        fields.add("subtitle")
    if ir.bullets:
        fields.add("bullets")
    if ir.metrics:
        fields.add("metrics")
    if ir.table is not None:
        fields.add("table")
    if ir.columns:
        fields.add("columns")
    if ir.note is not None and ir.note.strip():
        fields.add("note")
    return fields


def body_weight(ir: SlideIR) -> float:
    w = sum(text_weight(b) for b in ir.bullets)
    for c in ir.columns:
        w += text_weight(c.heading) + sum(text_weight(b) for b in c.bullets)
    for m in ir.metrics:
        w += text_weight(m.label) + text_weight(m.value) + text_weight(m.unit or "")
    return w


def _table_dims(ir: SlideIR) -> tuple[int, int]:
    """(总行数含表头, 列数)。"""
    t = ir.table
    if t is None:
        return 0, 0
    n_rows = len(t.rows) + (1 if t.header else 0)
    widths = [len(t.header)] + [len(r) for r in t.rows]
    return n_rows, max(widths) if widths else 0


def validate_slide(ir: SlideIR) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    add = lambda rule, detail: issues.append(
        ValidationIssue(rule=rule, page_no=ir.page_no, detail=detail)
    )
    L = LIMITS

    if not ir.title.strip():
        add(IssueCode.V_TITLE_LEN, "标题为空")
    elif text_weight(ir.title) > L["title_weight"]:
        add(IssueCode.V_TITLE_LEN, f"标题 {text_weight(ir.title):.0f} 超过 {L['title_weight']:.0f} 汉字当量")

    # toc 条目为章节名，豁免数量上限
    if ir.slide_type != SlideType.TOC and len(ir.bullets) > L["bullet_count"]:
        add(IssueCode.V_BULLET_COUNT, f"{len(ir.bullets)} 条 bullets 超过 {L['bullet_count']}")
    for b in ir.bullets:
        if text_weight(b) > L["bullet_weight"]:
            add(IssueCode.V_BULLET_LEN, f"bullet {text_weight(b):.0f} 汉字当量超限：{b[:20]}…")
            break

    if ir.metrics:
        n = len(ir.metrics)
        if not (L["metrics_min"] <= n <= L["metrics_max"]):
            add(IssueCode.V_METRICS, f"metrics 数量 {n} 不在 {L['metrics_min']}-{L['metrics_max']}")

    if ir.table is not None:
        rows, cols = _table_dims(ir)
        if rows > L["table_rows"] or cols > L["table_cols"]:
            add(IssueCode.V_TABLE_SIZE, f"表格 {rows}行×{cols}列 超过 {L['table_rows']}×{L['table_cols']}")

    allowed = BLOCK_WHITELIST[ir.slide_type]
    present = _present(ir)
    illegal = present - allowed
    if illegal:
        add(IssueCode.V_WHITELIST, f"页型 {ir.slide_type.value} 不允许字段：{sorted(illegal)}")

    required_missing: list[str] = []
    if ir.slide_type == SlideType.TOC and not ir.bullets:
        required_missing.append("bullets")
    if ir.slide_type == SlideType.TEXT_POINTS and not (ir.bullets or ir.metrics):
        required_missing.append("bullets/metrics")
    if ir.slide_type == SlideType.TWO_COLUMN and len(ir.columns) != 2:
        required_missing.append("columns(应为 2)")
    if ir.slide_type == SlideType.TABLE and ir.table is None:
        required_missing.append("table")
    if ir.slide_type == SlideType.KEY_METRICS and not ir.metrics:
        required_missing.append("metrics")
    if required_missing:
        add(IssueCode.V_WHITELIST, f"页型 {ir.slide_type.value} 缺少必填块：{required_missing}")

    if ir.slide_type not in _BUDGET_EXEMPT:
        bw = body_weight(ir)
        if bw > L["page_budget"]:
            add(IssueCode.V_PAGE_BUDGET, f"页面正文 {bw:.0f} 汉字当量超过 {L['page_budget']:.0f}")

    return issues


def validate_deck(deck: Deck) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for ir in deck.slides:
        issues.extend(validate_slide(ir))
    numbers = [ir.page_no for ir in deck.slides]
    if numbers != list(range(1, len(deck.slides) + 1)):
        add = lambda detail: issues.append(
            ValidationIssue(rule=IssueCode.V_PAGE_NO, page_no=0, detail=detail)
        )
        if len(numbers) != len(set(numbers)):
            add(f"page_no 重复：{numbers}")
        if numbers and sorted(numbers) != list(range(1, len(numbers) + 1)):
            add(f"page_no 不连续：{numbers}")
    return issues
