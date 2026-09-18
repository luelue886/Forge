from __future__ import annotations

import json

from app.pipeline.plan import assemble_full_plan, content_page_count, plan_slides
from app.pipeline.understand import understand
from app.schema.enums import SlideType, TableStrategy

SEC1 = json.dumps({
    "section_id": "sec-0001",
    "summary": "本章介绍项目覆盖范围与设备规模。",
    "key_facts": [
        {"text": "本项目覆盖 12 个园区，接入设备 8,600 台。", "block_ids": ["blk-0001"]},
        {"text": "这句话根本不存在于原文之中", "block_ids": []},
        {"text": "数据中台已完成", "block_ids": ["blk-0002"]},
    ],
    "table_digests": [],
}, ensure_ascii=False)

SEC2 = json.dumps({
    "section_id": "sec-0002",
    "summary": "季度指标达成情况。",
    "key_facts": [
        {"text": "综上，季度目标整体达成率 120%。", "block_ids": ["blk-0005"]},
    ],
    "table_digests": [
        {"table_id": "tbl-001", "topic": "Q3 关键指标", "headline_cells": [{"row": 1, "col": 2}],
         "suggested_strategy": "copy", "rationale": "3×3 小表"},
    ],
}, ensure_ascii=False)


def test_understand_filters_and_rebinds(basic_docx, fake_llm_factory):
    from app.parsing.docx_parser import parse_docx

    tree = parse_docx(basic_docx)
    client, fake = fake_llm_factory([SEC1, SEC2])
    docmap = understand(tree, client)

    assert docmap.doc_title == "智慧园区平台建设汇报"
    assert len(docmap.section_maps) == 2
    sm1 = docmap.section_maps[0]
    assert sm1.section_id == "sec-0001"
    texts = [f.text for f in sm1.key_facts]
    assert "本项目覆盖 12 个园区，接入设备 8,600 台。" in texts
    assert "这句话根本不存在于原文之中" not in texts  # 未命中原文 → 丢弃
    # prompt 里注入了代码预计算的表格统计
    prompt_all = json.dumps(fake.calls, ensure_ascii=False)
    assert "tbl-001" in prompt_all and "3 行" in prompt_all


def test_page_count_formula():
    assert content_page_count(0) == 5
    assert content_page_count(1500) == 5
    assert content_page_count(3000) == 10
    assert content_page_count(4500) == 15
    assert content_page_count(6000) == 20
    assert content_page_count(500000) == 20


PLAN_LLM = json.dumps({
    "title": "智慧园区平台建设汇报",
    "pages": [
        {"page_no": 1, "slide_type": "text_points", "title": "项目覆盖规模",
         "brief": "呈现园区与设备规模", "src_refs": ["blk-0001", "blk-fake-999"], "table_ref": None},
        {"page_no": 2, "slide_type": "text_points", "title": "建设进展",
         "brief": "数据中台完成", "src_refs": ["blk-0002"], "table_ref": None},
        {"page_no": 3, "slide_type": "text_points", "title": "达成情况",
         "brief": "总结", "src_refs": ["blk-0005"], "table_ref": None},
        {"page_no": 4, "slide_type": "text_points", "title": "多余页",
         "brief": "引用编造表格", "src_refs": [], "table_ref": {"table_id": "tbl-099", "strategy": "copy"}},
    ],
}, ensure_ascii=False)


def _tree_and_map(basic_docx):
    from app.parsing.docx_parser import parse_docx
    from app.schema.docmap import DocMap

    tree = parse_docx(basic_docx)
    docmap = DocMap.model_validate_json(json.dumps({
        "doc_title": "智慧园区平台建设汇报",
        "section_maps": [
            {"section_id": "sec-0001", "summary": "概述",
             "key_facts": [], "table_digests": []},
            {"section_id": "sec-0002", "summary": "指标",
             "key_facts": [],
             "table_digests": [{"table_id": "tbl-001", "topic": "Q3 指标",
                                "suggested_strategy": "copy", "rationale": "小表"}]},
        ],
    }, ensure_ascii=False))
    return tree, docmap


def test_plan_sanitize(basic_docx, fake_llm_factory):
    tree, docmap = _tree_and_map(basic_docx)
    client, _ = fake_llm_factory([PLAN_LLM])
    plan = plan_slides(tree, docmap, client)

    # 编造 ref 被过滤
    assert "blk-fake-999" not in [r for p in plan.pages for r in p.src_refs]
    # 编造 table_ref 被清空，缺失的 tbl-001 被自动补页
    table_pages = [p for p in plan.pages if p.table_ref is not None]
    assert len(table_pages) == 1
    tp = table_pages[0]
    assert tp.table_ref.table_id == "tbl-001" and tp.table_ref.strategy == TableStrategy.COPY
    # page_no 重排连续
    assert [p.page_no for p in plan.pages] == list(range(1, len(plan.pages) + 1))


def test_assemble_full_plan(basic_docx, fake_llm_factory):
    tree, docmap = _tree_and_map(basic_docx)
    client, _ = fake_llm_factory([PLAN_LLM])
    content = plan_slides(tree, docmap, client)
    full = assemble_full_plan(tree, content)

    types = [p.slide_type for p in full.pages]
    assert types[0] is SlideType.COVER and types[1] is SlideType.TOC
    assert types[-1] is SlideType.CLOSING
    # 两个 L1 章节各插一个章节头，位置在首个引用该章的页之前
    headers = [i for i, t in enumerate(types) if t is SlideType.SECTION_HEADER]
    assert len(headers) == 2
    assert full.pages[headers[0]].title == "一、项目概述"
    assert full.pages[headers[1]].title == "二、季度指标"
    # 目录之后、第一个章节头之前的页属于第一章
    assert headers[0] == 2
    assert headers[1] > headers[0]
    # 编号连续且封面标题回退正确
    assert [p.page_no for p in full.pages] == list(range(1, len(full.pages) + 1))
    assert full.pages[0].title == "智慧园区平台建设汇报"
