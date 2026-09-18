from __future__ import annotations

import itertools

import pytest

from app.pipeline.docfill import (
    DocFillError,
    assemble_docir,
    fill_all_sections,
    fill_section,
)
from app.pipeline.genre import extract_letter_frame
from app.schema.docir import HeadingBlock, ParaBlock, SectionIR, validate_docir
from app.schema.docplan import DocPlan, DocPlanItem
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
        _sec("sec-0001", 1, "一、总体情况", [_blk("营收 1,234.56 万元，同比增长 8%。")]),
        _sec("sec-0002", 2, "（一）回款明细", [
            _blk("回款周期变长。"),
            DocBlock(block_id="blk-9001", kind="table", table_id="tbl-001"),
        ]),
    ], tables=[DocTable(
        table_id="tbl-001", section_id="sec-0002", n_rows=2, n_cols=2,
        header=["项目", "金额"], rows=[["营收", "1,234.56 万元"]],
    )])


class FakeClient:
    """按调用次序弹出预置回复；schema 忽略（测试自行保证类型正确）。"""

    def __init__(self, replies):
        self.calls: list[tuple] = []
        self._replies = list(replies)

    def structured(self, schema, messages, stage=None, **kw):
        self.calls.append((schema, messages, stage))
        if not self._replies:
            raise AssertionError("FakeClient 收到了多余的调用")
        return self._replies.pop(0)


def _item(section_id: str, heading: str = "", level: int = 0, seq: int = 1,
          table_ids: list[str] | None = None,
          image_ids: list[str] | None = None) -> DocPlanItem:
    return DocPlanItem(seq=seq, section_id=section_id, heading=heading,
                       heading_level=level, src_refs=[section_id],
                       table_ids=table_ids or [], image_ids=image_ids or [])


# ---- fill_section ----

def test_fill_letter_body_paras_only():
    plan = DocPlan(genre=Genre.LETTER, title="调岗申请书",
                   items=[_item("sec-0000")])
    client = FakeClient([type("Out", (), {"paras": ["恳请批准调岗请求。"]})()])
    sec = fill_section(plan.items[0], plan, _letter_tree(), client)
    assert [b.kind for b in sec.blocks] == ["para"]
    assert sec.blocks[0].text == "恳请批准调岗请求。"
    assert len(client.calls) == 1
    assert client.calls[0][2] == "docfill/sec-0000"


def test_fill_letter_frame_leak_triggers_retry():
    plan = DocPlan(genre=Genre.LETTER, title="调岗申请书",
                   items=[_item("sec-0000")])

    class Out:
        def __init__(self, paras):
            self.paras = paras

    client = FakeClient([Out(["恳请批准。", "此致敬礼！"]),
                         Out(["恳请批准调岗。"])])
    sec = fill_section(plan.items[0], plan, _letter_tree(), client)
    assert len(client.calls) == 2  # 第一次混入框架行被拒，重试成功
    assert [b.text for b in sec.blocks] == ["恳请批准调岗。"]
    # 纠错反馈要回传给 LLM
    assert "拒收" in client.calls[1][1][1]["content"]


def test_fill_report_section_heading_and_paras():
    plan = DocPlan(genre=Genre.REPORT, title="运营报告",
                   items=[_item("sec-0001", "一、总体情况", 1)])

    class Out:
        heading = "一、总体经营情况"
        paras = ["营收 1,234.56 万元。"]

    sec = fill_section(plan.items[0], plan, _report_tree(), FakeClient([Out()]))
    assert [(b.kind, getattr(b, "level", None)) for b in sec.blocks] == \
        [("heading", 1), ("para", None)]
    assert sec.blocks[0].text == "一、总体经营情况"


def test_fill_heading_too_long_retries_then_fails():
    plan = DocPlan(genre=Genre.REPORT, title="运营报告",
                   items=[_item("sec-0001", "一、总体情况", 1)])
    long_heading = "一" * 26  # 26 汉字当量 > 25

    class Out:
        heading = long_heading
        paras = ["正文。"]

    with pytest.raises(DocFillError, match="两次填充均未通过校验"):
        fill_section(plan.items[0], plan, _report_tree(),
                     FakeClient([Out(), Out()]))


def test_fill_empty_prose_deterministic_no_llm():
    tree = _tree("员工信息登记表", root_blocks=[
        DocBlock(block_id="blk-9002", kind="table", table_id="tbl-002")],
        tables=[DocTable(table_id="tbl-002", section_id="sec-0001", n_rows=2,
                         n_cols=2, header=["a", "b"], rows=[["1", "2"]])])
    tree.sections[0].subsections.append(
        _sec("sec-0001", 1, "（一）明细",
             [DocBlock(block_id="blk-9003", kind="table", table_id="tbl-002")]))
    # 根节只有表格块 → 前言零 LLM；纯表格子节 → 仅标题照抄
    plan = DocPlan(genre=Genre.FORM, title="员工信息登记表", items=[
        _item("sec-0000", seq=1), _item("sec-0001", "（一）明细", 1, seq=2)])
    client = FakeClient([])
    root_sec = fill_section(plan.items[0], plan, tree, client)
    sub_sec = fill_section(plan.items[1], plan, tree, client)
    assert root_sec.blocks == [] and len(client.calls) == 0
    assert [(b.kind, b.text) for b in sub_sec.blocks] == [("heading", "（一）明细")]


# ---- fill_all_sections（断点续跑 + 进度）----

def test_fill_all_sections_resume_skips_done(tmp_path):
    tree = _report_tree()
    plan = DocPlan(genre=Genre.REPORT, title="运营报告", items=[
        _item("sec-0001", "一、总体情况", 1, seq=1),
        _item("sec-0002", "（一）回款明细", 2, seq=2, table_ids=["tbl-001"]),
    ])
    # sec-0001 已落盘 → 只 fill sec-0002
    done = SectionIR(section_id="sec-0001", blocks=[])
    (tmp_path / "sec_01.ir.json").write_text(
        done.model_dump_json(indent=2), encoding="utf-8")

    class Out:
        heading = "（一）回款明细改写"
        paras = ["回款周期变长。"]

    progress = []
    result = fill_all_sections(plan, tree, FakeClient([Out()]), tmp_path,
                               on_progress=lambda d, t: progress.append((d, t)))
    assert set(result) == {"sec-0001", "sec-0002"}
    assert result["sec-0001"] == done
    assert result["sec-0002"].blocks[0].text == "（一）回款明细改写"
    assert (tmp_path / "sec_02.ir.json").exists()
    assert progress[-1] == (2, 2)


def test_fill_all_sections_parallel_all_new(tmp_path):
    tree = _report_tree()
    plan = DocPlan(genre=Genre.REPORT, title="运营报告", items=[
        _item("sec-0001", "一、总体情况", 1, seq=1),
        _item("sec-0002", "（一）回款明细", 2, seq=2, table_ids=["tbl-001"]),
    ])

    class Out:
        def __init__(self, heading):
            self.heading = heading
            self.paras = ["段落。"]

    client = FakeClient([Out("一、总体情况改"), Out("（一）回款明细改")])
    result = fill_all_sections(plan, tree, client, tmp_path)
    assert len(client.calls) == 2
    assert all((tmp_path / f"sec_{i:02d}.ir.json").exists() for i in (1, 2))
    assert set(result) == {"sec-0001", "sec-0002"}


# ---- assemble_docir ----

def test_assemble_letter_full_frame():
    tree = _letter_tree()
    plan = DocPlan(genre=Genre.LETTER, title="调岗申请书",
                   items=[_item("sec-0000")])
    sections = {"sec-0000": SectionIR(section_id="sec-0000",
                                      blocks=[ParaBlock(text="恳请批准调岗。")])}

    doc = assemble_docir(plan, sections, tree, extract_letter_frame(tree))
    kinds = [b.kind for b in doc.blocks]
    assert kinds == ["doc_title", "salutation", "para", "closing", "signature"]
    assert doc.blocks[1].text == "尊敬的公司领导："
    assert doc.blocks[3].lines == ["此致", "敬礼！"]
    assert doc.blocks[4].signer == "申请人：王明"
    assert doc.blocks[4].date == "2026 年 9 月 18 日"
    assert [i for i in validate_docir(doc)
            if i.rule.value != "W_DOC_PARA_LONG"] == []


def test_assemble_report_level_norm_and_tables():
    tree = _report_tree()
    plan = DocPlan(genre=Genre.REPORT, title="2026 年第三季度运营报告", items=[
        _item("sec-0001", "一、总体情况", 1, seq=1),
        _item("sec-0002", "（一）回款明细", 2, seq=2, table_ids=["tbl-001"]),
    ])
    sections = {
        "sec-0001": SectionIR(section_id="sec-0001", blocks=[
            HeadingBlock(level=1, text="一、总体情况"),
            ParaBlock(text="营收 1,234.56 万元。")]),
        "sec-0002": SectionIR(section_id="sec-0002", blocks=[
            HeadingBlock(level=2, text="（一）回款明细"),
            ParaBlock(text="回款周期变长。")]),
    }
    doc = assemble_docir(plan, sections, tree)
    kinds = [b.kind for b in doc.blocks]
    assert kinds == ["doc_title", "heading", "para", "heading", "para", "table"]
    tbl = doc.blocks[-1]
    assert tbl.table_id == "tbl-001" and tbl.header == ["项目", "金额"]
    assert tbl.rows == [["营收", "1,234.56 万元"]]  # verbatim
    assert validate_docir(doc) == []


def test_assemble_applies_table_rewrites():
    # 复用 _report_tree 的 tbl-001：header + rows=[["营收","1,234.56 万元"]]
    tree = _report_tree()
    plan = DocPlan(genre=Genre.REPORT, title="运营报告", items=[
        _item("sec-0001", "一、总体情况", 1, seq=1),
        _item("sec-0002", "（一）回款明细", 2, seq=2, table_ids=["tbl-001"]),
    ])
    sections = {"sec-0001": SectionIR(section_id="sec-0001", blocks=[]),
                "sec-0002": SectionIR(section_id="sec-0002", blocks=[
                    HeadingBlock(level=2, text="（一）回款明细")])}
    doc = assemble_docir(plan, sections, tree, table_rewrites={"tbl-001": {
        "0,0": "营收规模",
        "0,1": "营业收入 1,234.56 万元已经达成",
        "5,5": "越界忽略",
        "x,y": "非法键忽略",
    }})
    tbl = doc.blocks[-1]
    assert tbl.header == ["项目", "金额"]  # 表头永不改写
    assert tbl.rows == [["营收规模", "营业收入 1,234.56 万元已经达成"]]
    assert tbl.src_index == tree.tables[0].src_index


def test_assemble_first_heading_level2_promoted():
    # 源结构被钳制后首个 heading 是 level 2 → 组装时提为 1，避免跳级
    tree = _tree("季度报告", subs=[_sec("sec-0001", 3, "（一）明细", [_blk("数据。")])])
    plan = DocPlan(genre=Genre.REPORT, title="季度报告",
                   items=[_item("sec-0001", "（一）明细", 2, seq=1)])
    sections = {"sec-0001": SectionIR(section_id="sec-0001", blocks=[
        HeadingBlock(level=2, text="（一）明细"),
        ParaBlock(text="数据。")])}
    doc = assemble_docir(plan, sections, tree)
    assert doc.blocks[1].level == 1
    assert validate_docir(doc) == []


def test_assemble_missing_section_raises():
    plan = DocPlan(genre=Genre.REPORT, title="报告",
                   items=[_item("sec-0001", "一、", 1, seq=1)])
    with pytest.raises(DocFillError, match="缺少节 fill 产物"):
        assemble_docir(plan, {}, _tree("报告"))


def test_assemble_appends_images_after_tables():
    from app.schema.doctree import DocImage

    tree = _tree("人事信息", subs=[
        _sec("sec-0001", 1, "一、花名册", [
            _blk("人员构成如下。"),
            DocBlock(block_id="blk-9001", kind="table", table_id="tbl-001"),
            DocBlock(block_id="blk-9002", kind="image", image_id="img-001"),
        ]),
    ], tables=[DocTable(table_id="tbl-001", section_id="sec-0001",
                        n_rows=1, n_cols=1, rows=[["数据"]])])
    tree.images.append(DocImage(image_id="img-001", section_id="sec-0001",
                                body_index=3, cx_emu=2160000, cy_emu=1440000))
    plan = DocPlan(genre=Genre.FORM, title="人事信息", items=[
        _item("sec-0001", "一、花名册", 1, seq=1,
              table_ids=["tbl-001"], image_ids=["img-001"]),
    ])
    sections = {"sec-0001": SectionIR(section_id="sec-0001", blocks=[
        HeadingBlock(level=1, text="一、花名册"),
        ParaBlock(text="人员构成如下。")])}

    doc = assemble_docir(plan, sections, tree)
    kinds = [b.kind for b in doc.blocks]
    assert kinds == ["doc_title", "heading", "para", "table", "image"]
    assert doc.blocks[-1].body_index == 3
    assert validate_docir(doc) == []


def test_assemble_missing_image_raises():
    plan = DocPlan(genre=Genre.FORM, title="表", items=[
        _item("sec-0001", "一、", 1, seq=1, image_ids=["img-404"])])
    sections = {"sec-0001": SectionIR(section_id="sec-0001",
                                      blocks=[ParaBlock(text="内容。")])}
    with pytest.raises(DocFillError, match="不存在的图片"):
        assemble_docir(plan, sections, _tree("表"))
