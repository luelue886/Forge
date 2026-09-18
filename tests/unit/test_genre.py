from __future__ import annotations

import itertools

from app.pipeline.docplan import build_doc_plan
from app.pipeline.genre import detect_genre, extract_letter_frame
from app.schema.doctree import DocBlock, DocMeta, DocSection, DocTable, DocTree
from app.schema.enums import Genre

_n = itertools.count(1)


def _blk(text: str, kind: str = "para") -> DocBlock:
    return DocBlock(block_id=f"blk-{next(_n):04d}", kind=kind, text=text)


def _sec(sid: str, level: int, title: str, blocks=(), subs=()) -> DocSection:
    return DocSection(section_id=sid, level=level, title=title,
                      blocks=list(blocks), subsections=list(subs))


def _tree(title: str, root_blocks=(), subs=(), tables=()) -> DocTree:
    root = _sec("sec-0000", 0, title, root_blocks, subs)
    parts = [title]
    for sec in root.walk():
        if sec is not root:
            parts.append(sec.title)
        parts.extend(b.text for b in sec.blocks if b.text)
        for b in sec.blocks:
            if b.kind == "table":
                t = next(t for t in tables if t.table_id == b.table_id)
                parts.append(t.flat_text())
    return DocTree(
        meta=DocMeta(source_format="docx", source_name="fixture.docx",
                     n_chars=len("\n".join(parts))),
        sections=[root], tables=list(tables), full_text="\n".join(parts),
    )


def _letter_tree() -> DocTree:
    return _tree("调岗申请书", root_blocks=[
        _blk("尊敬的公司领导："),
        _blk("您好！我于 2023 年 3 月入职现岗位，现申请调岗。"),
        _blk("恳请领导批准。"),
        _blk("此致"),
        _blk("敬礼！"),
        _blk("申请人：王明"),
        _blk("2026 年 9 月 18 日"),
    ])


def _report_tree() -> DocTree:
    return _tree("2026 年第三季度运营报告", subs=[
        _sec("sec-0001", 1, "一、总体情况", [_blk("营收 1,234.56 万元。")]),
        _sec("sec-0002", 1, "二、风险分析", [_blk("回款周期变长。")]),
    ])


def _form_tree() -> DocTree:
    tbl = DocTable(
        table_id="tbl-001", section_id="sec-0000", n_rows=5, n_cols=3,
        header=["姓名", "部门", "联系电话"],
        rows=[[f"员工{i:02d}", "市场部", f"1380000000{i}"] for i in range(1, 5)],
    )
    return _tree("员工信息登记表",
                 root_blocks=[_blk("请如实填写以下信息。"),
                              DocBlock(block_id="blk-9001", kind="table",
                                       table_id="tbl-001")],
                 tables=[tbl])


# ---- 体裁识别 ----

def test_detect_letter():
    s = detect_genre(_letter_tree())
    assert s.genre is Genre.LETTER
    assert s.confidence >= 0.9


def test_detect_report():
    s = detect_genre(_report_tree())
    assert s.genre is Genre.REPORT
    assert s.confidence >= 0.7


def test_detect_form_by_table_ratio():
    s = detect_genre(_form_tree())
    assert s.genre is Genre.FORM
    assert s.confidence >= 0.6


def test_detect_ambiguous_low_confidence():
    t = _tree("材料汇编", root_blocks=[_blk("第一部分内容说明。"), _blk("第二部分内容说明。")])
    s = detect_genre(t)
    assert s.confidence < 0.5


def test_extract_letter_frame_verbatim():
    tree = _letter_tree()
    f = extract_letter_frame(tree)
    assert f.salutation == "尊敬的公司领导："
    assert f.closing == ["此致", "敬礼！"]
    assert f.signer == "申请人：王明"
    assert f.date == "2026 年 9 月 18 日"


def test_extract_letter_frame_absent():
    f = extract_letter_frame(_report_tree())
    assert f.salutation is None and not f.closing and f.signer is None


# ---- DocPlan ----

def test_plan_letter_single_root_item():
    plan = build_doc_plan(_letter_tree())
    assert plan.genre is Genre.LETTER
    assert len(plan.items) == 1
    it = plan.items[0]
    assert it.heading == "" and it.heading_level == 0
    assert it.src_refs == ["sec-0000"]


def test_plan_report_follows_structure():
    tree = _report_tree()
    tree.sections[0].blocks.append(_blk("引言段落。"))
    plan = build_doc_plan(tree)
    assert plan.genre is Genre.REPORT
    assert [it.heading for it in plan.items] == ["", "一、总体情况", "二、风险分析"]
    assert plan.items[1].src_refs == ["sec-0001"]
    assert plan.title == "2026 年第三季度运营报告"


def test_plan_table_ids_wired_and_level_clamped():
    tbl = DocTable(table_id="tbl-002", section_id="sec-0001", n_rows=2, n_cols=2,
                   header=["a", "b"], rows=[["1", "2"]])
    tree = _tree("季度报告", subs=[
        _sec("sec-0001", 3, "（一）明细", [
            _blk("数据如下。"),
            DocBlock(block_id="blk-9002", kind="table", table_id="tbl-002"),
        ]),
    ], tables=[tbl])
    plan = build_doc_plan(tree)
    assert plan.items[0].heading_level == 2  # level 3 → 钳制到 2
    assert plan.items[0].table_ids == ["tbl-002"]


def test_plan_llm_fallback_when_rules_unsure():
    tree = _tree("材料汇编", root_blocks=[_blk("第一部分内容说明。")])

    class FakeClient:
        def structured(self, schema, messages, stage=None, **kw):
            return schema(genre=Genre.LETTER, reason="称呼与落款特征")

    plan = build_doc_plan(tree, client=FakeClient())
    assert plan.genre is Genre.LETTER


def test_plan_llm_failure_falls_back_to_rules():
    tree = _tree("材料汇编", root_blocks=[_blk("第一部分内容说明。")])

    class BadClient:
        def structured(self, *a, **kw):
            raise RuntimeError("LLM 不可用")

    plan = build_doc_plan(tree, client=BadClient())
    assert plan.genre is Genre.REPORT  # 无特征时默认 report
