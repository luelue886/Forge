from __future__ import annotations

from pydantic import BaseModel, Field

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.schema.doctree import DocTree
from app.schema.enums import SlideType
from app.schema.plan import SlidePlanItem
from app.schema.slideir import (
    LIMITS,
    Column,
    Metric,
    SlideIR,
    TableIR,
    validate_slide,
)

CONTENT_TYPES = {SlideType.TEXT_POINTS, SlideType.TWO_COLUMN, SlideType.KEY_METRICS}


class FillError(Exception):
    pass


class _TextPointsOut(BaseModel):
    title: str
    bullets: list[str] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    note: str | None = None


class _TwoColumnOut(BaseModel):
    title: str
    columns: list[Column] = Field(default_factory=list)
    note: str | None = None


class _KeyMetricsOut(BaseModel):
    title: str
    metrics: list[Metric] = Field(default_factory=list)
    bullets: list[str] = Field(default_factory=list)
    note: str | None = None


_OUT_SCHEMA = {
    SlideType.TEXT_POINTS: _TextPointsOut,
    SlideType.TWO_COLUMN: _TwoColumnOut,
    SlideType.KEY_METRICS: _KeyMetricsOut,
}


def fill_page(item: SlidePlanItem, tree: DocTree, client: LLMClient,
              pm: PromptManager | None = None) -> SlideIR:
    """单页规划 → SlideIR。结构页与表格页零 LLM；内容页 LLM 仿写 + 一次纠错重试。"""
    if item.slide_type in CONTENT_TYPES:
        return _fill_llm(item, tree, client, pm)
    return fill_deterministic(item, tree)


def fill_deterministic(item: SlidePlanItem, tree: DocTree,
                       toc_entries: list[str] | None = None,
                       doc_title: str = "") -> SlideIR:
    t = item.slide_type
    if t is SlideType.COVER:
        return SlideIR(page_no=item.page_no, slide_type=t, title=item.title,
                       subtitle=item.brief or "", src_refs=item.src_refs)
    if t is SlideType.TOC:
        if not toc_entries:
            raise FillError("TOC 页需要传入章节条目（toc_entries）")
        return SlideIR(page_no=item.page_no, slide_type=t, title="目录",
                       bullets=list(toc_entries), src_refs=item.src_refs)
    if t is SlideType.SECTION_HEADER:
        return SlideIR(page_no=item.page_no, slide_type=t, title=item.title,
                       subtitle=item.brief or "", src_refs=item.src_refs)
    if t is SlideType.CLOSING:
        return SlideIR(page_no=item.page_no, slide_type=t, title="谢谢观看",
                       subtitle=doc_title, src_refs=item.src_refs)
    if t is SlideType.TABLE:
        return _fill_table(item, tree)
    raise FillError(f"未知页型：{t}")


def _fill_table(item: SlidePlanItem, tree: DocTree) -> SlideIR:
    if item.table_ref is None:
        raise FillError(f"p{item.page_no} 表格页缺少 table_ref")
    t = next((x for x in tree.tables if x.table_id == item.table_ref.table_id), None)
    if t is None:
        raise FillError(f"p{item.page_no} 引用了不存在的表 {item.table_ref.table_id}")

    header, rows = list(t.header), [list(r) for r in t.rows]  # verbatim
    note = None
    max_rows = LIMITS["table_rows"] - (1 if header else 0)
    if len(rows) > max_rows or len(header) > LIMITS["table_cols"]:
        note = (f"原表 {t.n_rows} 行 × {t.n_cols} 列，超出单页容量，"
                f"此处展示前 {min(len(rows), max_rows)} 行 / 前 {LIMITS['table_cols']} 列，完整数据见源文档。")
        header = header[: LIMITS["table_cols"]]
        rows = [r[: LIMITS["table_cols"]] for r in rows[:max_rows]]

    return SlideIR(
        page_no=item.page_no, slide_type=SlideType.TABLE, title=item.title,
        table=TableIR(table_id=t.table_id, header=header, rows=rows),
        note=note, src_refs=item.src_refs or [t.table_id],
    )


def _fill_llm(item: SlidePlanItem, tree: DocTree, client: LLMClient,
              pm: PromptManager | None) -> SlideIR:
    pm = pm or PromptManager()
    source_text = tree.resolve(item.src_refs) or tree.sections[0].title
    user = pm.render("fill/user.j2", page_no=item.page_no,
                     slide_type=item.slide_type.value, title=item.title,
                     brief=item.brief, source_text=source_text, errors="")
    messages = [
        {"role": "system", "content": pm.render("fill/system.md")},
        {"role": "user", "content": user},
    ]
    schema = _OUT_SCHEMA[item.slide_type]

    errors = ""
    for _attempt in (1, 2):
        out = client.structured(schema, messages, stage=f"fill/p{item.page_no}")
        ir = SlideIR(page_no=item.page_no, slide_type=item.slide_type,
                     src_refs=item.src_refs, **out.model_dump())
        issues = validate_slide(ir)
        if not issues:
            return ir
        errors = "\n".join(f"- [{i.rule.value}] {i.detail}" for i in issues)
        messages = [
            messages[0],
            {"role": "user", "content": pm.render(
                "fill/user.j2", page_no=item.page_no, slide_type=item.slide_type.value,
                title=item.title, brief=item.brief, source_text=source_text, errors=errors)},
        ]
    raise FillError(f"p{item.page_no} 两次填充均未通过校验：\n{errors}")
