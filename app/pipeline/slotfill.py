"""槽填充：计划页 → PageFill（真模板页的槽位填充/删除指令）。

表格页零 LLM 确定性生成（verbatim 天然合规）；其余页（内容页 + 封面/目录/
章节/结尾结构页）LLM 提案 → 槽级校验 + 铁律复检（数字溯源 / 10-gram 防抄
袭，与 e2e_ppt 同语义）→ 1 轮定向重试 → 降级。结构页降级走确定性兜底
（天然合规），内容页降级为 title-only 页 + 源文 verbatim 备注；结构页 LLM
硬异常也走确定性兜底，不杀 job。三铁律不变：事实 verbatim、不发明、
校验不过降级不阻断。
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.qa.ngram import exempt_from_tree, ngram_hits, strip_facts
from app.qa.numbers import check_numbers
from app.schema.doctree import DocTree
from app.schema.enums import IssueCode, SlideType
from app.schema.plan import SlidePlanItem
from app.schema.slotfill import PageFill, TableIR, ValidationIssue
from app.schema.textlen import text_weight
from app.schema.tplcatalog import SlideInfo, SlotInfo

CONTENT_TYPES = {SlideType.TEXT_POINTS, SlideType.TWO_COLUMN, SlideType.KEY_METRICS}
STRUCTURAL_TYPES = {SlideType.COVER, SlideType.TOC, SlideType.SECTION_HEADER,
                    SlideType.CLOSING}
NOTE_WEIGHT_MAX = 300.0
TABLE_MAX_ROWS = 5  # 含表头行
TABLE_MAX_COLS = 6
_UNTOUCHABLE = ("design", "page_no")


class FillError(Exception):
    pass


class _SlotFillOut(BaseModel):
    title: str
    slots: dict[str, str] = Field(default_factory=dict)
    deletes: list[str] = Field(default_factory=list)
    note: str | None = None


# ---------- 槽位选择 ----------

def _fillable(cat_slide: SlideInfo) -> list[SlotInfo]:
    """非 design/page_no 且容量≥3 的可填槽（文档序）。"""
    return [s for s in cat_slide.slots
            if s.role not in _UNTOUCHABLE and s.capacity >= 3]


def _title_slot(cat_slide: SlideInfo) -> SlotInfo | None:
    """title 角色槽（任意容量）；无 title 槽时用最大字号可填槽承载。"""
    for s in cat_slide.slots:
        if s.role == "title":
            return s
    fill = _fillable(cat_slide)
    return max(fill, key=lambda s: s.font_pt) if fill else None


def _others(cat_slide: SlideInfo, keep: set[str]) -> list[str]:
    """非 design/page_no、未保留的槽 id（含小容量）——待删除，杜绝模板原文遗留。"""
    return [s.slot_id for s in cat_slide.slots
            if s.role not in _UNTOUCHABLE and s.slot_id not in keep]


def _truncate_weight(text: str, cap: float) -> str:
    """按汉字当量截断到容量内。"""
    out: list[str] = []
    w = 0.0
    for ch in text:
        cw = text_weight(ch)
        if w + cw > cap:
            break
        out.append(ch)
        w += cw
    return "".join(out)


# ---------- 页面分派 ----------

def slotfill_page(item: SlidePlanItem, tree: DocTree, cat_slide: SlideInfo,
                  client: LLMClient, pm: PromptManager | None = None,
                  facts: set[str] | None = None,
                  toc_entries: list[str] | None = None,
                  doc_title: str = "") -> PageFill:
    """单页规划 → PageFill。表格页零 LLM；其余页 LLM 提案 + 1 轮重试 + 降级。

    结构页（封面/目录/章节/结尾）LLM 失败——两轮未过或硬异常——兜底
    slotfill_deterministic（现成的副题/条目分配/文档题策略，天然合规）；
    内容页降级为 title-only 页。最坏退回确定性结构页，不会比零 LLM 更差。

    facts：docmap key_facts 原文串集合——铁律 verbatim 的事实跨度，
    ngram 比对前剥离（两律相撞不算抄袭）。缺省 None = 空集（仅测试桩场景）。
    toc_entries/doc_title：结构页兜底与 LLM 提示共用（目录条目 / 文档题）。
    """
    if item.slide_type is SlideType.TABLE:
        return slotfill_deterministic(item, tree, cat_slide)
    structural = item.slide_type in STRUCTURAL_TYPES
    try:
        return _fill_llm(item, tree, cat_slide, client, pm, facts,
                         toc_entries=toc_entries, doc_title=doc_title)
    except Exception:
        if structural:
            return slotfill_deterministic(item, tree, cat_slide,
                                          toc_entries, doc_title)
        raise


# ---------- 确定性填充（结构页兜底路径 + 表格页主路径，零 LLM） ----------

def slotfill_deterministic(item: SlidePlanItem, tree: DocTree, cat_slide: SlideInfo,
                           toc_entries: list[str] | None = None,
                           doc_title: str = "") -> PageFill:
    t = item.slide_type
    base = dict(page_no=item.page_no, slide_type=t,
                template_slide=cat_slide.slide_no, title=item.title,
                src_refs=item.src_refs)
    if t is SlideType.COVER:
        return _fill_cover(item, cat_slide, base)
    if t is SlideType.TOC:
        return _fill_toc(item, cat_slide, toc_entries or [], base)
    if t is SlideType.SECTION_HEADER:
        return _fill_section(item, cat_slide, base)
    if t is SlideType.CLOSING:
        return _fill_closing(item, cat_slide, doc_title, base)
    if t is SlideType.TABLE:
        return _fill_table(item, tree, cat_slide, base)
    raise FillError(f"p{item.page_no} 页型 {t} 不能确定性填充")


def _page(cat_slide: SlideInfo, base: dict, values: dict[str, str],
          note: str | None = None, table: TableIR | None = None) -> PageFill:
    return PageFill(**base, slot_values=values,
                    slot_deletes=_others(cat_slide, set(values)),
                    note=note, table=table)


def _fill_cover(item: SlidePlanItem, cat_slide: SlideInfo, base: dict) -> PageFill:
    title = _title_slot(cat_slide)
    if title is None:
        raise FillError(f"p{item.page_no} 映射页 S{cat_slide.slide_no} 完全无可用槽")
    values = {title.slot_id: _truncate_weight(item.title, title.capacity)}
    rest = [s for s in _fillable(cat_slide) if s.slot_id != title.slot_id]
    if rest and item.brief.strip():
        sub = max(rest, key=lambda s: s.capacity)  # 次大非 design 槽 ← 副题
        values[sub.slot_id] = _truncate_weight(item.brief, sub.capacity)
    return _page(cat_slide, base, values)


def _fill_toc(item: SlidePlanItem, cat_slide: SlideInfo, entries: list[str],
              base: dict) -> PageFill:
    title = _title_slot(cat_slide)
    if title is None:
        raise FillError(f"p{item.page_no} 映射页 S{cat_slide.slide_no} 完全无可用槽")
    values = {title.slot_id: _truncate_weight("目录", title.capacity)}
    rest = [s for s in _fillable(cat_slide) if s.slot_id != title.slot_id]
    bodies = [s for s in rest if s.role == "body"] or (
        [max(rest, key=lambda s: s.capacity)] if rest else [])
    if bodies and entries:
        for i, e in enumerate(entries):
            j = min(i, len(bodies) - 1)  # 超出条目并入末槽
            prev = values.get(bodies[j].slot_id)
            values[bodies[j].slot_id] = e if prev is None else f"{prev}\n{e}"
        last = bodies[-1]
        values[last.slot_id] = _truncate_weight(values[last.slot_id], last.capacity)
    return _page(cat_slide, base, values)


def _fill_section(item: SlidePlanItem, cat_slide: SlideInfo, base: dict) -> PageFill:
    title = _title_slot(cat_slide)
    if title is None:
        raise FillError(f"p{item.page_no} 映射页 S{cat_slide.slide_no} 完全无可用槽")
    values = {title.slot_id: _truncate_weight(item.title, title.capacity)}
    return _page(cat_slide, base, values)


def _fill_closing(item: SlidePlanItem, cat_slide: SlideInfo, doc_title: str,
                  base: dict) -> PageFill:
    title = _title_slot(cat_slide)
    if title is None:
        raise FillError(f"p{item.page_no} 映射页 S{cat_slide.slide_no} 完全无可用槽")
    values = {title.slot_id: _truncate_weight("谢谢观看", title.capacity)}
    rest = [s for s in _fillable(cat_slide) if s.slot_id != title.slot_id]
    if rest and doc_title:
        sub = max(rest, key=lambda s: s.capacity)  # 次大非 design 槽 ← 文档题
        values[sub.slot_id] = _truncate_weight(doc_title, sub.capacity)
    return _page(cat_slide, base, values)


def _fill_table(item: SlidePlanItem, tree: DocTree, cat_slide: SlideInfo,
                base: dict) -> PageFill:
    if item.table_ref is None:
        raise FillError(f"p{item.page_no} 表格页缺少 table_ref")
    t = next((x for x in tree.tables if x.table_id == item.table_ref.table_id), None)
    if t is None:
        raise FillError(f"p{item.page_no} 引用了不存在的表 {item.table_ref.table_id}")

    header, rows = list(t.header), [list(r) for r in t.rows]  # verbatim
    note = None
    max_rows = TABLE_MAX_ROWS - (1 if header else 0)
    if len(rows) > max_rows or len(header) > TABLE_MAX_COLS:
        note = (f"原表 {t.n_rows} 行 × {t.n_cols} 列，超出单页容量，"
                f"此处展示前 {min(len(rows), max_rows)} 行 / 前 {TABLE_MAX_COLS} 列，完整数据见源文档。")
        header = header[:TABLE_MAX_COLS]
        rows = [r[:TABLE_MAX_COLS] for r in rows[:max_rows]]

    values: dict[str, str] = {}
    title = _title_slot(cat_slide)
    if title is not None:
        values[title.slot_id] = _truncate_weight(item.title, title.capacity)
    return _page(cat_slide, base, values, note=note,
                 table=TableIR(table_id=t.table_id, header=header, rows=rows))


# ---------- LLM 填充（内容页 + 结构页） ----------

def _slot_digest(cat_slide: SlideInfo) -> list[dict]:
    return [{
        "slot_id": s.slot_id, "role": s.role, "capacity": round(s.capacity),
        "font_pt": s.font_pt, "box": f"({s.x},{s.y}) {s.w}×{s.h}",
        "sample": s.sample,
    } for s in cat_slide.slots if s.role not in _UNTOUCHABLE]


def _fill_llm(item: SlidePlanItem, tree: DocTree, cat_slide: SlideInfo,
              client: LLMClient, pm: PromptManager | None,
              facts: set[str] | None = None,
              toc_entries: list[str] | None = None,
              doc_title: str = "") -> PageFill:
    pm = pm or PromptManager()
    if not _fillable(cat_slide):
        raise FillError(f"p{item.page_no} 映射页 S{cat_slide.slide_no} 完全无可用槽")
    structural = item.slide_type in STRUCTURAL_TYPES
    source_text = tree.resolve(item.src_refs) or tree.sections[0].title

    errors = ""
    for _attempt in (1, 2):
        user = pm.render(
            "slotfill/user.j2",
            page_no=item.page_no, slide_type=item.slide_type.value,
            title=item.title, brief=item.brief,
            slide_no=cat_slide.slide_no, layout=cat_slide.layout,
            slots=_slot_digest(cat_slide),
            source_text=source_text, errors=errors,
            doc_title=doc_title if item.slide_type in (SlideType.COVER,
                                                       SlideType.CLOSING) else "",
            toc_entries=(toc_entries or []) if item.slide_type is SlideType.TOC else [])
        messages = [
            {"role": "system", "content": pm.render("slotfill/system.md")},
            {"role": "user", "content": user},
        ]
        out = client.structured(_SlotFillOut, messages, stage=f"slotfill/p{item.page_no}")
        fill = PageFill(page_no=item.page_no, slide_type=item.slide_type,
                        template_slide=cat_slide.slide_no, title=out.title,
                        slot_values=out.slots, slot_deletes=out.deletes,
                        note=out.note, src_refs=item.src_refs)
        issues = validate_page_fill(fill, cat_slide) + _semantic_issues(
            fill, tree, facts, skip_ngram=structural)
        if not issues:
            return fill
        errors = "\n".join(f"- [{i.rule.value}] {i.detail}" for i in issues)
    if structural:
        # 结构页兜底：确定性策略天然合规，最坏退回零 LLM 行为
        return slotfill_deterministic(item, tree, cat_slide, toc_entries, doc_title)
    return _degrade(item, cat_slide, source_text)


def _semantic_issues(fill: PageFill, tree: DocTree,
                     facts: set[str] | None,
                     skip_ngram: bool = False) -> list[ValidationIssue]:
    """铁律复检（与 e2e_ppt 同语义）：数字溯源 + 10-gram 防抄袭（事实跨度先剥离）。

    只在 LLM 路径调用——表格页由确定性代码 verbatim 生成天然合规；
    备注允许引用原文，跳过（对齐 e2e_ppt 复检范围）。结构页跳过 ngram：
    目录条目/文档题/章节题合法回源文标题，照内容页口径会误杀；数字溯源
    不放宽——页码/条目里的数字仍须 verbatim。
    """
    facts = facts or set()
    exempt = exempt_from_tree(tree)
    issues: list[ValidationIssue] = []
    for sid, text in sorted(fill.slot_values.items()):
        for tok, ctx in check_numbers(text, tree.full_text):
            issues.append(ValidationIssue(
                rule=IssueCode.E_NUM_UNTRACED, page_no=fill.page_no,
                detail=f"槽 {sid} 数字 {tok} 无法溯源（须原文 verbatim）：{ctx}"))
        if skip_ngram:
            continue
        for g in ngram_hits(strip_facts(text, facts), tree.full_text, exempt=exempt):
            issues.append(ValidationIssue(
                rule=IssueCode.E_PLAGIARISM, page_no=fill.page_no,
                detail=f"槽 {sid} 与源文连续雷同（须改写）：{g}"))
    return issues


def _degrade(item: SlidePlanItem, cat_slide: SlideInfo, source_text: str) -> PageFill:
    """两轮提案均未过 → title-only 页：计划题截到标题槽容量，备注放源文前 300 当量 verbatim。"""
    title = _title_slot(cat_slide)
    if title is None:
        raise FillError(f"p{item.page_no} 映射页 S{cat_slide.slide_no} 完全无可用槽")
    values = {title.slot_id: _truncate_weight(item.title, title.capacity)}
    return PageFill(
        page_no=item.page_no, slide_type=item.slide_type,
        template_slide=cat_slide.slide_no, title=item.title,
        slot_values=values, slot_deletes=_others(cat_slide, set(values)),
        note=_truncate_weight(source_text, NOTE_WEIGHT_MAX),
        src_refs=item.src_refs)


# ---------- 校验 ----------

def validate_page_fill(fill: PageFill, cat_slide: SlideInfo) -> list[ValidationIssue]:
    """槽级校验：未知/不可触碰槽、逐槽容量、内容页覆盖、title 必填、note 长度。"""
    issues: list[ValidationIssue] = []
    by_id = {s.slot_id: s for s in cat_slide.slots}
    touched = set(fill.slot_values) | set(fill.slot_deletes)

    def add(rule: IssueCode, detail: str) -> None:
        issues.append(ValidationIssue(rule=rule, page_no=fill.page_no, detail=detail))

    overlap = set(fill.slot_values) & set(fill.slot_deletes)
    if overlap:
        add(IssueCode.V_SLOT_UNKNOWN, f"槽同时被填充与删除：{sorted(overlap)}")
    for sid in sorted(touched):
        s = by_id.get(sid)
        if s is None:
            add(IssueCode.V_SLOT_UNKNOWN, f"槽 {sid} 不存在于 S{cat_slide.slide_no}")
        elif s.role in _UNTOUCHABLE:
            add(IssueCode.V_SLOT_UNKNOWN, f"槽 {sid}（{s.role} 位）不可触碰")
    for sid, text in sorted(fill.slot_values.items()):
        s = by_id.get(sid)
        if s is None or s.role in _UNTOUCHABLE:
            continue
        w = text_weight(text)
        if w > s.capacity:
            add(IssueCode.V_SLOT_CAP,
                f"槽 {sid}（{s.role} 容量 {s.capacity:.0f}）文本 {w:.0f} 当量超容")
    missing = [s.slot_id for s in cat_slide.slots
               if s.role not in _UNTOUCHABLE and s.slot_id not in touched]
    if missing:
        add(IssueCode.V_SLOT_COVER, f"槽未覆盖（须填充或删除）：{missing}")
    titles = [s for s in cat_slide.slots if s.role == "title"]
    if titles:
        tv = fill.slot_values.get(titles[0].slot_id, "")
        if not tv.strip():
            add(IssueCode.V_SLOT_TITLE, f"title 槽 {titles[0].slot_id} 必须非空填充")
    if fill.note and text_weight(fill.note) > NOTE_WEIGHT_MAX:
        add(IssueCode.V_NOTE_LEN,
            f"备注 {text_weight(fill.note):.0f} 当量超过 {NOTE_WEIGHT_MAX:.0f}")
    return issues
