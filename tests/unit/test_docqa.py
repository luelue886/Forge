from __future__ import annotations

import itertools

from app.qa.docqa import check_sections, qa_and_repair
from app.schema.docplan import DocPlan, DocPlanItem
from app.schema.docir import ParaBlock, SectionIR
from app.schema.doctree import DocBlock, DocMeta, DocSection, DocTree
from app.schema.enums import Genre

_n = itertools.count(1)


def _blk(text: str) -> DocBlock:
    return DocBlock(block_id=f"blk-{next(_n):04d}", kind="para", text=text)


def _item(sid: str, heading: str, seq: int) -> DocPlanItem:
    return DocPlanItem(seq=seq, section_id=sid, heading=heading,
                       heading_level=1, src_refs=[sid])


def _qa_tree() -> DocTree:
    root = DocSection(section_id="sec-0000", level=0, title="生产工作总结")
    root.subsections = [
        DocSection(section_id="sec-0001", level=1, title="一、生产情况",
                   blocks=[_blk("该项目获得省级专项资金支持，用于产线改造。")]),
        DocSection(section_id="sec-0002", level=1, title="二、财务情况",
                   blocks=[_blk("全年营收 1,234.56 万元。")]),
    ]
    parts = [root.title]
    for s in root.subsections:
        parts += [s.title, *[b.text for b in s.blocks]]
    return DocTree(
        meta=DocMeta(source_format="docx", source_name="f.docx",
                     n_chars=len("\n".join(parts))),
        sections=[root], tables=[], full_text="\n".join(parts),
    )


def _plan() -> DocPlan:
    return DocPlan(genre=Genre.REPORT, title="生产工作总结", items=[
        _item("sec-0001", "一、生产情况", 1),
        _item("sec-0002", "二、财务情况", 2),
    ])


def _sections() -> dict[str, SectionIR]:
    return {
        "sec-0001": SectionIR(section_id="sec-0001", blocks=[
            ParaBlock(text="全年生产 666 台设备。"),          # 编造数字
            ParaBlock(text="该项目获得省级专项资金支持。"),  # 13 字 verbatim 抄袭
        ]),
        "sec-0002": SectionIR(section_id="sec-0002", blocks=[
            ParaBlock(text="报告期内实现营业收入一千二百三十四点五六万元。"),  # 合规改写
        ]),
    }


class FakeClient:
    def __init__(self, replies):
        self.calls: list[tuple] = []
        self._replies = list(replies)

    def structured(self, schema, messages, stage=None, **kw):
        self.calls.append((schema, messages, stage))
        r = self._replies.pop(0)

        class _Out:
            heading = r[0]
            paras = list(r[1])
        return _Out()


def test_check_sections_flags_only_bad_section():
    issues = check_sections(_plan(), _qa_tree(), _sections())
    codes = sorted((i.section_id, i.code.value) for i in issues)
    assert ("sec-0001", "E-NUM-UNTRACED") in codes
    assert ("sec-0001", "E-PLAGIARISM") in codes
    assert all(i.section_id == "sec-0001" for i in issues)  # sec-0002 干净


def test_check_sections_no_cross_block_false_positive():
    # 段落边界拼接伪 10-gram：单块各 ≤9 字重合、拼接后才 ≥10，逐块检查不得误报
    root = DocSection(section_id="sec-0000", level=0, title="工作报告")
    root.subsections = [DocSection(
        section_id="sec-0001", level=1, title="一、市场情况",
        blocks=[_blk("本季度整体呈现向好态势。华南市场布局基本完成。")])]
    parts = [root.title, root.subsections[0].title,
             *[b.text for b in root.subsections[0].blocks]]
    tree = DocTree(
        meta=DocMeta(source_format="docx", source_name="f.docx",
                     n_chars=len("\n".join(parts))),
        sections=[root], tables=[], full_text="\n".join(parts))
    plan = DocPlan(genre=Genre.REPORT, title="工作报告",
                   items=[_item("sec-0001", "一、市场情况", 1)])
    sections = {"sec-0001": SectionIR(section_id="sec-0001", blocks=[
        ParaBlock(text="整体呈现向好态势。"),
        ParaBlock(text="华南市场布局已然完成。"),
    ])}
    assert check_sections(plan, tree, sections) == []


def test_qa_and_repair_refills_only_bad_section(tmp_path):
    client = FakeClient([("一、生产情况改写",
                          ["全年设备生产任务顺利完成，产能保持稳定。"])])
    sections = _sections()
    result, issues = qa_and_repair(_plan(), _qa_tree(), sections, client,
                                   sections_dir=tmp_path)
    assert issues == []
    assert len(client.calls) == 1  # 只重 fill 了 sec-0001
    assert client.calls[0][2] == "docfill/sec-0001"
    # 违规明细作为纠错上下文回传；抄袭问题附可操作修正指引
    user_msg = client.calls[0][1][1]["content"]
    assert "666" in user_msg and "E-NUM-UNTRACED" in user_msg
    assert "调换语序" in user_msg
    assert result["sec-0001"].blocks[0].text == "一、生产情况改写"
    assert result["sec-0002"] is sections["sec-0002"]  # 好节未动
    assert (tmp_path / "sec_01.ir.json").exists()


def test_qa_and_repair_keeps_old_version_on_refill_failure():
    class BadClient:
        def structured(self, *a, **kw):
            raise RuntimeError("LLM 不可用")

    sections = _sections()
    result, issues = qa_and_repair(_plan(), _qa_tree(), sections, BadClient())
    assert issues  # 残余问题照常上报
    assert result["sec-0001"] is sections["sec-0001"]  # 旧版保留
