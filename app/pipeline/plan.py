from __future__ import annotations

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.schema.docmap import DocMap
from app.schema.doctree import DocTree
from app.schema.enums import SlideType, TableStrategy
from app.schema.plan import SlidePlan, SlidePlanItem, TableRef

CONTENT_MIN, CONTENT_MAX = 5, 20


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
                pm: PromptManager | None = None) -> SlidePlan:
    """LLM 规划内容页；后置代码消毒：src_refs 硬过滤、表格去向唯一、缺表补页。"""
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
    )
    messages = [
        {"role": "system", "content": pm.render("planner/system.md", n_content_pages=n_content)},
        {"role": "user", "content": user},
    ]
    plan = client.structured(SlidePlan, messages, stage="planner")
    return _sanitize(plan, tree)


def _sanitize(plan: SlidePlan, tree: DocTree) -> SlidePlan:
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


def assemble_full_plan(tree: DocTree, content: SlidePlan) -> SlidePlan:
    """确定性装配：封面 + 目录 + 章节头（首个引用落入该章时）+ 内容页 + 结束页。"""
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
    return SlidePlan(title=content.title or root.title, pages=pages)
