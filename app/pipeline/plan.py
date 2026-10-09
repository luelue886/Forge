from __future__ import annotations

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.schema.docmap import DocMap
from app.schema.doctree import DocTree
from app.schema.enums import SlideType, TableStrategy
from app.schema.plan import SlidePlan, SlidePlanItem, TableRef
from app.schema.tplcatalog import SlideInfo, TemplateCatalog

CONTENT_MIN, CONTENT_MAX = 5, 20
_TEXT_TYPES = frozenset((SlideType.TEXT_POINTS, SlideType.TWO_COLUMN,
                         SlideType.KEY_METRICS))


def content_page_count(n_chars: int) -> int:
    """内容页数 = clamp(round(n_chars/300), 5, 20)。"""
    return max(CONTENT_MIN, min(CONTENT_MAX, round(n_chars / 300)))


def _block_ranges(tree: DocTree) -> list[dict]:
    """每节的可用引用 ID 概览（blk 连续段压缩成 range 表示）。"""
    root = tree.sections[0]
    out: list[dict] = []
    for sec in root.walk():
        ids = [b.block_id for b in sec.blocks]
        ranges: list[str] = []
        if ids:
            start = prev = int(ids[0].split("-")[1])
            for bid in ids[1:]:
                n = int(bid.split("-")[1])
                if n == prev + 1:
                    prev = n
                else:
                    ranges.append(f"blk-{start:04d}~blk-{prev:04d}")
                    start = prev = n
            ranges.append(f"blk-{start:04d}~blk-{prev:04d}")
        out.append({
            "id": sec.section_id,
            "level": sec.level,
            "title": sec.title,
            "block_ranges": "、".join(ranges) if ranges else "（无内容块）",
        })
    return out


def _maps_digest(docmap: DocMap) -> list[dict]:
    return [{
        "section_id": m.section_id,
        "summary": m.summary,
        "fact_samples": "；".join(f.text for f in m.key_facts[:5]),
    } for m in docmap.section_maps]


def plan_slides(tree: DocTree, docmap: DocMap, client: LLMClient,
                pm: PromptManager | None = None,
                catalog: TemplateCatalog | None = None) -> SlidePlan:
    """LLM 规划内容页（有目录时同时指派 template_slide）；
    后置代码消毒：src_refs 硬过滤、表格去向唯一、缺表补页、模板页号界内过滤。"""
    pm = pm or PromptManager()
    n_content = content_page_count(tree.meta.n_chars)
    tables = [{
        "table_id": t.table_id,
        "section_id": t.section_id,
        "topic": (next((d.topic for m in docmap.section_maps
                        for d in m.table_digests if d.table_id == t.table_id), t.caption or "表格")),
        "n_rows": t.n_rows,
        "n_cols": t.n_cols,
        "suggested_strategy": next(
            (d.suggested_strategy.value for m in docmap.section_maps
             for d in m.table_digests if d.table_id == t.table_id), "copy"),
        "rationale": next(
            (d.rationale for m in docmap.section_maps
             for d in m.table_digests if d.table_id == t.table_id), ""),
    } for t in tree.tables]

    user = pm.render(
        "planner/user.j2",
        doc_title=docmap.doc_title,
        n_content_pages=n_content,
        structure=_block_ranges(tree),
        tables=tables,
        maps=_maps_digest(docmap),
        tpl_pool=_pool_digest(catalog),
    )
    messages = [
        {"role": "system", "content": pm.render(
            "planner/system.md", n_content_pages=n_content, has_tpl=catalog is not None)},
        {"role": "user", "content": user},
    ]
    plan = client.structured(SlidePlan, messages, stage="planner")
    return _sanitize(plan, tree, catalog)


def _sanitize(plan: SlidePlan, tree: DocTree,
              catalog: TemplateCatalog | None = None) -> SlidePlan:
    known = tree.known_refs()
    tids = {t.table_id: t for t in tree.tables}

    pages: list[SlidePlanItem] = []
    seen_tables: set[str] = set()
    for p in plan.pages:
        p.src_refs = [r for r in p.src_refs if r in known]
        if p.table_ref is not None and p.table_ref.table_id not in tids:
            p.table_ref = None
        if p.table_ref is not None:
            if p.table_ref.table_id in seen_tables:
                p.table_ref = None  # 一表一去向，后到者让位
            else:
                seen_tables.add(p.table_ref.table_id)
        # LLM 模板页号：无目录 → 一律清空；有目录 → 仅界内保留（池合规交给 assign 校验）
        p.template_slide = (
            p.template_slide if catalog is not None and p.template_slide is not None
            and 1 <= p.template_slide <= catalog.n_slides else None)
        pages.append(p)

    for tid, t in tids.items():
        if tid in seen_tables:
            continue
        strategy = TableStrategy.COPY if (t.n_rows <= 5 and t.n_cols <= 6) else TableStrategy.SPLIT
        pages.append(SlidePlanItem(
            page_no=0,
            slide_type=SlideType.TABLE,
            title=(t.caption or "数据表")[:20],
            brief="planner 未覆盖该表，代码自动补页",
            src_refs=[tid],
            table_ref=TableRef(table_id=tid, strategy=strategy),
        ))

    for i, p in enumerate(pages, 1):
        p.page_no = i
    plan.pages = pages
    return plan


def _block_section_map(tree: DocTree) -> tuple[dict[str, str], dict[str, str]]:
    """(block_id→section_id, section_id→最近的 L1 祖先)。"""
    root = tree.sections[0]
    blk2sec: dict[str, str] = {}
    sec2l1: dict[str, str] = {root.section_id: root.section_id}

    def rec(sec: object, l1: str) -> None:
        for b in sec.blocks:
            blk2sec[b.block_id] = sec.section_id
        for sub in sec.subsections:
            my = sub.section_id if sub.level == 1 else (l1 or root.section_id)
            sec2l1[sub.section_id] = my
            rec(sub, my)

    rec(root, root.section_id)
    return blk2sec, sec2l1


def assemble_full_plan(tree: DocTree, content: SlidePlan,
                       catalog: TemplateCatalog | None = None) -> SlidePlan:
    """确定性装配：封面 + 目录 + 章节头（首个引用落入该章时）+ 内容页 + 结束页。
    有目录时结构页/表格页的 template_slide 同步确定性指派；文本内容页保留 LLM 提案。"""
    blk2sec, sec2l1 = _block_section_map(tree)
    tid2sec = {t.table_id: t.section_id for t in tree.tables}

    def section_of(ref: str) -> str | None:
        if ref.startswith("sec-"):
            return sec2l1.get(ref)
        if ref.startswith("blk-"):
            return sec2l1.get(blk2sec.get(ref, ""))
        if ref.startswith("tbl-"):
            return sec2l1.get(tid2sec.get(ref, ""))
        return None

    root = tree.sections[0]
    pages: list[SlidePlanItem] = [
        SlidePlanItem(page_no=1, slide_type=SlideType.COVER,
                      title=(content.title or root.title)[:20], brief=""),
        SlidePlanItem(page_no=2, slide_type=SlideType.TOC, title="目录", brief=""),
    ]

    announced: set[str] = set()
    l1_titles = {s.section_id: s.title for s in root.walk() if s.level == 1}
    for p in content.pages:
        sec = next((section_of(r) for r in p.src_refs if section_of(r)), None)
        if sec and sec not in announced and sec in l1_titles:
            announced.add(sec)
            pages.append(SlidePlanItem(page_no=0, slide_type=SlideType.SECTION_HEADER,
                                       title=l1_titles[sec][:20], brief=""))
        pages.append(p)

    pages.append(SlidePlanItem(page_no=0, slide_type=SlideType.CLOSING, title="谢谢观看", brief=""))
    for i, p in enumerate(pages, 1):
        p.page_no = i
    if catalog is not None:
        assign_structural_slides(pages, catalog)
    return SlidePlan(title=content.title or root.title, pages=pages)


# ---------- 模板页指派 ----------

def _pool_digest(catalog: TemplateCatalog | None) -> list[dict]:
    """planner prompt 的候选池摘要（仅 content 池；无目录为空列表）。"""
    if catalog is None:
        return []
    return [{"signature": s.signature, "fillable": s.fillable}
            for s in _content_pool(catalog)]


def _content_pool(catalog: TemplateCatalog) -> list[SlideInfo]:
    """文本内容页的候选/轮换池：content 池 → 非 cover/closing 且 fillable∈[3,15] → 最大可填页。"""
    pool = [s for s in catalog.slides if s.role_hint == "content"]
    if not pool:
        pool = [s for s in catalog.slides
                if s.role_hint not in ("cover", "closing") and 3 <= s.fillable <= 15]
    if not pool:
        pool = [max(catalog.slides, key=lambda s: s.fillable)]
    return pool


def _assignable(slide_no: int, catalog: TemplateCatalog) -> bool:
    """文本内容页目标合规：界内、非 cover/closing、content 池或 fillable∈[3,15]。"""
    if not 1 <= slide_no <= catalog.n_slides:
        return False
    s = catalog.slide(slide_no)
    if s.role_hint in ("cover", "closing"):
        return False
    return s.role_hint == "content" or 3 <= s.fillable <= 15


def assign_structural_slides(pages: list[SlidePlanItem], catalog: TemplateCatalog) -> None:
    """结构页/表格页确定性指派（就地）：
    cover→S1；toc→toc 池否则 S2；closing→末页；section/table→各自池轮换否则内容池轮换。"""
    n = catalog.n_slides
    sec_pool = [s.slide_no for s in catalog.slides if s.role_hint == "section"]
    tbl_pool = [s.slide_no for s in catalog.slides if s.role_hint == "table_host"]
    toc_pool = [s.slide_no for s in catalog.slides if s.role_hint == "toc"]
    content = [s.slide_no for s in _content_pool(catalog)]
    si = ti = 0
    for p in pages:
        if p.slide_type is SlideType.COVER:
            p.template_slide = 1
        elif p.slide_type is SlideType.TOC:
            p.template_slide = toc_pool[0] if toc_pool else min(2, n)
        elif p.slide_type is SlideType.CLOSING:
            p.template_slide = n
        elif p.slide_type is SlideType.SECTION_HEADER:
            p.template_slide = (sec_pool[si % len(sec_pool)] if sec_pool
                                else content[si % len(content)])
            si += 1
        elif p.slide_type is SlideType.TABLE:
            p.template_slide = (tbl_pool[ti % len(tbl_pool)] if tbl_pool
                                else content[ti % len(content)])
            ti += 1


def assign_template_slides(plan: SlidePlan, catalog: TemplateCatalog) -> SlidePlan:
    """文本内容页的模板页号终审（就地+返回）：LLM 提案合规则保留，
    缺失/越界/落在 cover/closing/小容量页 → 内容池轮换兜底。"""
    pool = [s.slide_no for s in _content_pool(catalog)]
    rot = 0
    for p in plan.pages:
        if p.slide_type not in _TEXT_TYPES:
            continue
        if p.template_slide is None or not _assignable(p.template_slide, catalog):
            p.template_slide = pool[rot % len(pool)]
            rot += 1
    return plan
