"""M4 模板页指派：结构页确定性指派 + 内容页 LLM 提案校验/轮换兜底 + prompt 注入。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.plan import (
    _content_pool, assemble_full_plan, assign_structural_slides,
    assign_template_slides, plan_slides,
)
from app.render.tpl_catalog import extract_catalog
from app.schema.enums import SlideType
from app.schema.plan import SlidePlan, SlidePlanItem
from app.schema.tplcatalog import SlideInfo, SlotInfo, TemplateCatalog
from tests.mk_tpl import build_synthetic_template


@pytest.fixture(scope="module")
def catalog(tmp_path_factory) -> TemplateCatalog:
    tpl = build_synthetic_template(tmp_path_factory.mktemp("tpl") / "synthetic.pptx")
    return extract_catalog(tpl, "合成测试模板")


def _plan(pages: list[SlidePlanItem]) -> SlidePlan:
    return SlidePlan(title="t", pages=pages)


def _text_page(no: int, tpl_slide: int | None = None) -> SlidePlanItem:
    return SlidePlanItem(page_no=no, slide_type=SlideType.TEXT_POINTS, title=f"页{no}",
                         brief="", template_slide=tpl_slide)


class TestPools:
    def test_content_pool(self, catalog: TemplateCatalog):
        # 合成模板 S2/S3 是 content；S1 cover、S4 table_host(小容量)、S5 closing 不入池
        assert [s.slide_no for s in _content_pool(catalog)] == [2, 3]


class TestStructuralAssignment:
    def test_assemble_assigns_structural(self, catalog: TemplateCatalog):
        pages = [
            SlidePlanItem(page_no=1, slide_type=SlideType.COVER, title="t"),
            SlidePlanItem(page_no=2, slide_type=SlideType.TOC, title="目录"),
            SlidePlanItem(page_no=3, slide_type=SlideType.SECTION_HEADER, title="章一"),
            _text_page(4, tpl_slide=3),
            SlidePlanItem(page_no=5, slide_type=SlideType.TABLE, title="表",
                          src_refs=["tbl-001"]),
            SlidePlanItem(page_no=6, slide_type=SlideType.SECTION_HEADER, title="章二"),
            _text_page(7),
            SlidePlanItem(page_no=8, slide_type=SlideType.CLOSING, title="谢谢观看"),
        ]
        plan = _plan(pages)
        assign_structural_slides(pages, catalog)
        assert pages[0].template_slide == 1  # cover → S1
        assert pages[1].template_slide == 2  # toc 宿主：首个 title+body 可填页
        assert pages[7].template_slide == 5  # closing → 末页
        # section 池空 → 内容池轮换：首个→S2，次个→S3
        assert pages[2].template_slide == 2
        assert pages[5].template_slide == 3
        # 表格页 → table_host 池（S4）
        assert pages[4].template_slide == 4
        # 文本页提案不被结构指派覆盖
        assert pages[3].template_slide == 3
        assert pages[6].template_slide is None

    def test_assemble_full_plan_with_catalog(self, basic_docx, fake_llm_factory,
                                             catalog: TemplateCatalog):
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
        llm = json.dumps({
            "title": "智慧园区平台建设汇报",
            "pages": [
                {"page_no": 1, "slide_type": "text_points", "title": "覆盖规模",
                 "brief": "b", "src_refs": ["blk-0001"], "table_ref": None,
                 "template_slide": 3},
                {"page_no": 2, "slide_type": "text_points", "title": "进展",
                 "brief": "b", "src_refs": ["blk-0002"], "table_ref": None,
                 "template_slide": 99},  # 越界提案
            ],
        }, ensure_ascii=False)
        client, _ = fake_llm_factory([llm])
        content = plan_slides(tree, docmap, client, catalog=catalog)
        # 越界提案在 sanitize 阶段已被清空
        by_title = {p.title: p for p in content.pages}
        assert by_title["覆盖规模"].template_slide == 3
        assert by_title["进展"].template_slide is None
        # _sanitize 自动补的表格页（planner 未覆盖 tbl-001）无提案
        tbl_pages = [p for p in content.pages if p.table_ref is not None]
        assert tbl_pages[0].template_slide is None

        full = assemble_full_plan(tree, content, catalog)
        assign_template_slides(full, catalog)
        # 结构页确定性指派
        types = [p.slide_type for p in full.pages]
        assert full.pages[0].template_slide == 1  # cover
        assert full.pages[1].template_slide == 2  # toc → S2
        assert full.pages[-1].template_slide == 5  # closing → 末页
        # 合规提案保留
        assert by_title["覆盖规模"].template_slide == 3
        # 越界提案 → 内容池轮换兜底（首个轮换位 = S2）
        assert by_title["进展"].template_slide == 2
        # 表格页 → table_host S4
        tbl = next(p for p in full.pages if p.table_ref is not None)
        assert tbl.template_slide == 4
        # 每页都有模板页号（构建前置条件）
        assert all(p.template_slide is not None for p in full.pages)


class TestTocHosts:
    """M6：toc 宿主选择——toc 池空时不得盲选 S2（真模板 S2 可能无任何可填槽）。"""

    def test_synthetic_toc_picks_s2(self, catalog: TemplateCatalog):
        from app.pipeline.plan import _toc_hosts

        assert _toc_hosts(catalog)[0] == 2  # S2 有 title + 可填 body

    def test_fallback_skips_unusable_s2(self):
        def slot(sid: str, role: str, cap: float) -> SlotInfo:
            return SlotInfo(slot_id=sid, path=[0], role=role, x=1.0, y=1.0,
                            w=4.0, h=1.0, font_pt=18.0, sample="样本", capacity=cap)

        def slide(no: int, slots: list[SlotInfo], hint: str = "") -> SlideInfo:
            return SlideInfo(slide_no=no, layout="L", signature=f"S{no}", slots=slots,
                             fillable=sum(1 for s in slots if s.fillable), role_hint=hint)

        # 办公技巧实况：S2 是纯构图 table_host（全 design/page_no，无 title）
        cat = TemplateCatalog(
            stem="toc-fallback", display_name="目录回退", n_slides=5, slides=[
                slide(1, [slot("0", "title", 20), slot("1", "body", 30)], "cover"),
                slide(2, [slot("0", "design", 2), slot("1", "page_no", 2)]),
                slide(3, [slot("0", "title", 10), slot("1", "body", 60)]),
                slide(4, [slot("0", "title", 10)]),  # 无可填 body，不能承载目录
                slide(5, [slot("0", "title", 10), slot("1", "body", 30)], "closing"),
            ])
        from app.pipeline.plan import _toc_hosts

        assert _toc_hosts(cat) == [3]
        pages = [SlidePlanItem(page_no=1, slide_type=SlideType.TOC, title="目录")]
        assign_structural_slides(pages, cat)
        assert pages[0].template_slide == 3


class TestAssignValidation:
    def test_invalid_targets_rotated(self, catalog: TemplateCatalog):
        pages = [
            _text_page(1, tpl_slide=99),   # 越界
            _text_page(2, tpl_slide=1),    # cover 页不可承载内容
            _text_page(3, tpl_slide=5),    # closing 页不可承载内容
            _text_page(4, tpl_slide=4),    # table_host 且 fillable=1，不合规则
            _text_page(5, tpl_slide=3),    # 合规保留
            _text_page(6),                 # 缺失
            _text_page(7),                 # 缺失（轮换到不同页）
        ]
        plan = assign_template_slides(_plan(pages), catalog)
        ts = [p.template_slide for p in plan.pages]
        # 轮换池 [2,3]：非法/缺失按序取 S2、S3、S2、S3
        assert ts == [2, 3, 2, 3, 3, 2, 3]

    def test_non_text_untouched(self, catalog: TemplateCatalog):
        pages = [SlidePlanItem(page_no=1, slide_type=SlideType.COVER, title="t",
                               template_slide=None)]
        assign_template_slides(_plan(pages), catalog)
        assert pages[0].template_slide is None  # 结构页归 assign_structural_slides 管


class TestPromptInjection:
    def test_pool_in_prompt(self, basic_docx, fake_llm_factory, catalog: TemplateCatalog):
        from app.parsing.docx_parser import parse_docx
        from app.schema.docmap import DocMap

        tree = parse_docx(basic_docx)
        docmap = DocMap.model_validate_json(json.dumps({
            "doc_title": "t", "section_maps": [
                {"section_id": "sec-0001", "summary": "s", "key_facts": [],
                 "table_digests": []}],
        }, ensure_ascii=False))
        client, fake = fake_llm_factory([json.dumps({
            "title": "t", "pages": [
                {"page_no": 1, "slide_type": "text_points", "title": "a",
                 "brief": "", "src_refs": ["blk-0001"], "table_ref": None}],
        }, ensure_ascii=False)])
        plan_slides(tree, docmap, client, catalog=catalog)
        prompt_all = json.dumps(fake.calls, ensure_ascii=False)
        assert "模板页候选池" in prompt_all
        assert "template_slide" in prompt_all
        # 池里只列 content 页（S2/S3 签名），不含 cover/结尾页
        assert "S3 布局=" in prompt_all
        assert "S5 布局=" not in prompt_all

    def test_no_catalog_no_pool(self, basic_docx, fake_llm_factory):
        from app.parsing.docx_parser import parse_docx
        from app.schema.docmap import DocMap

        tree = parse_docx(basic_docx)
        docmap = DocMap.model_validate_json(json.dumps({
            "doc_title": "t", "section_maps": [
                {"section_id": "sec-0001", "summary": "s", "key_facts": [],
                 "table_digests": []}],
        }, ensure_ascii=False))
        client, fake = fake_llm_factory([json.dumps({
            "title": "t", "pages": [
                {"page_no": 1, "slide_type": "text_points", "title": "a",
                 "brief": "", "src_refs": ["blk-0001"], "table_ref": None,
                 "template_slide": 2}],  # 无目录时即使 LLM 给了页号也要清空
        }, ensure_ascii=False)])
        content = plan_slides(tree, docmap, client)
        assert all(p.template_slide is None for p in content.pages)
        prompt_all = json.dumps(fake.calls, ensure_ascii=False)
        assert "模板页候选池" not in prompt_all
