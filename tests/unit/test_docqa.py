from __future__ import annotations

import itertools

from app.qa.docqa import _RepairOut, check_sections, qa_and_repair
from app.schema.docplan import DocPlan, DocPlanItem
from app.schema.docir import HeadingBlock, ParaBlock, SectionIR
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
        return self._replies.pop(0)


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


def test_qa_and_repair_repairs_only_flagged_blocks(tmp_path):
    reply = _RepairOut(blocks={
        "0": "全年设备生产任务按计划达成，产能情况保持稳定。",
        "1": "省级专项资金已拨付到位，专项用于产线改造项目。",
    })
    client = FakeClient([reply])
    sections = _sections()
    result, issues = qa_and_repair(_plan(), _qa_tree(), sections, client,
                                   sections_dir=tmp_path)
    assert issues == []
    assert len(client.calls) == 1  # 只修了 sec-0001
    assert client.calls[0][2] == "docrepair/sec-0001"
    # 违规块原文与明细送修；干净块/干净节不送
    user_msg = client.calls[0][1][1]["content"]
    assert "666" in user_msg and "E-NUM-UNTRACED" in user_msg
    assert "省级专项资金支持" in user_msg and "E-PLAGIARISM" in user_msg
    assert "打散" in user_msg  # 抄袭问题附可操作修正指引
    assert "1,234.56" not in user_msg
    blocks = result["sec-0001"].blocks
    assert blocks[0].text == "全年设备生产任务按计划达成，产能情况保持稳定。"
    assert blocks[1].text == "省级专项资金已拨付到位，专项用于产线改造项目。"
    assert result["sec-0002"] is sections["sec-0002"]  # 好节未动
    assert (tmp_path / "sec_01.ir.json").exists()


def test_qa_and_repair_second_round_only_sends_remaining_block():
    # 第 1 轮修好块 0、块 1 仍脏；第 2 轮只送块 1（已通过的块绝不再送）
    client = FakeClient([
        _RepairOut(blocks={"0": "全年设备生产任务按计划达成。",
                           "1": "该项目获得省级专项资金支持，用于产线改造。"}),
        _RepairOut(blocks={"1": "省级专项资金已拨付到位，用于产线的全面改造。"}),
    ])
    result, issues = qa_and_repair(_plan(), _qa_tree(), _sections(), client)
    assert issues == []
    assert len(client.calls) == 2
    round2 = client.calls[1][1][1]["content"]
    assert "全年设备生产任务" not in round2  # 块 0 不再送修
    assert "省级专项资金支持" in round2      # 只剩块 1
    assert result["sec-0001"].blocks[0].text == "全年设备生产任务按计划达成。"


def test_qa_and_repair_rejects_unflagged_and_still_dirty():
    # sec-0001 块 0（编造数字）改写合规 → 采纳；块 1 原文照抄回来 → 拒收保留旧版
    reply = _RepairOut(blocks={
        "0": "全年设备生产任务按计划达成。",
        "1": "该项目获得省级专项资金支持，用于产线改造。",
        "2": "越权改写不存在的块",      # 无此块
    })
    sections = _sections()
    result, issues = qa_and_repair(_plan(), _qa_tree(), sections, FakeClient([reply]))
    assert [i.code.value for i in issues] == ["E-PLAGIARISM"]
    blocks = result["sec-0001"].blocks
    assert blocks[0].text == "全年设备生产任务按计划达成。"
    assert blocks[1].text == sections["sec-0001"].blocks[1].text  # 旧版保留


def test_qa_and_repair_clean_block_never_sent_or_replaced():
    # 干净块不送修，即使被 LLM 越权返回改写也不采纳
    root = DocSection(section_id="sec-0000", level=0, title="市场报告")
    root.subsections = [DocSection(
        section_id="sec-0001", level=1, title="一、市场情况",
        blocks=[_blk("本季度华南市场布局基本完成，各项指标运行平稳。")])]
    parts = [root.title, root.subsections[0].title,
             *[b.text for b in root.subsections[0].blocks]]
    tree = DocTree(
        meta=DocMeta(source_format="docx", source_name="f.docx",
                     n_chars=len("\n".join(parts))),
        sections=[root], tables=[], full_text="\n".join(parts))
    plan = DocPlan(genre=Genre.REPORT, title="市场报告",
                   items=[_item("sec-0001", "一、市场情况", 1)])
    sections = {"sec-0001": SectionIR(section_id="sec-0001", blocks=[
        ParaBlock(text="整体呈现向好态势。"),                # 干净（idx 0）
        ParaBlock(text="本季度华南市场布局基本完成。"),      # 12 字雷同（idx 1）
    ])}
    client = FakeClient([_RepairOut(blocks={
        "0": "越权改写干净块。",
        "1": "华南市场的布局工作在本季度内已然收官。",
    })])
    result, issues = qa_and_repair(plan, tree, sections, client)
    assert issues == []
    assert result["sec-0001"].blocks[0].text == "整体呈现向好态势。"  # 未动
    assert result["sec-0001"].blocks[1].text == "华南市场的布局工作在本季度内已然收官。"
    user_msg = client.calls[0][1][1]["content"]
    assert "整体呈现向好态势" not in user_msg  # 干净块不送修
    assert "本季度华南市场布局基本完成" in user_msg


def test_qa_and_repair_heading_overlong_rejected():
    # 标题雷同送修，但改写后 >25 汉字当量 → 拒收，保留旧标题（残留上报）
    h = "一、智慧办公平台推广项目实施进展说明"
    root = DocSection(section_id="sec-0000", level=0, title="工作总结")
    root.subsections = [DocSection(section_id="sec-0001", level=1, title=h,
                                   blocks=[_blk("正文段落与源文不雷同。")])]
    parts = [root.title, root.subsections[0].title,
             *[b.text for b in root.subsections[0].blocks]]
    tree = DocTree(
        meta=DocMeta(source_format="docx", source_name="f.docx",
                     n_chars=len("\n".join(parts))),
        sections=[root], tables=[], full_text="\n".join(parts))
    plan = DocPlan(genre=Genre.REPORT, title="工作总结",
                   items=[_item("sec-0001", h, 1)])
    sections = {"sec-0001": SectionIR(section_id="sec-0001", blocks=[
        HeadingBlock(level=1, text=h),  # 与源标题整句雷同
        ParaBlock(text="该节内容已按新口径梳理完成。"),
    ])}
    overlong = "关于智慧办公平台在市场端的推广实施情况以及各阶段目标完成度的综合性说明"
    result, issues = qa_and_repair(plan, tree, sections,
                                   FakeClient([_RepairOut(blocks={"0": overlong})]),
                                   max_rounds=1)
    assert [i.code.value for i in issues] == ["E-PLAGIARISM"]
    assert result["sec-0001"].blocks[0].text == h  # 旧标题保留


def test_qa_and_repair_accepts_prefixed_block_keys(tmp_path):
    # LLM 实测会把键写成模板式 "块 3"（e2e 20260918-171627 复盘），必须同 "3" 一样采纳
    reply = _RepairOut(blocks={
        "块 0": "全年设备生产任务按计划达成。",
        "块1": "省级专项资金已拨付到位，专项用于产线改造项目。",
    })
    client = FakeClient([reply])
    result, issues = qa_and_repair(_plan(), _qa_tree(), _sections(), client,
                                   sections_dir=tmp_path)
    assert issues == []
    blocks = result["sec-0001"].blocks
    assert blocks[0].text == "全年设备生产任务按计划达成。"
    assert blocks[1].text == "省级专项资金已拨付到位，专项用于产线改造项目。"


def test_qa_and_repair_keeps_old_version_on_client_failure():
    class BadClient:
        def structured(self, *a, **kw):
            raise RuntimeError("LLM 不可用")

    sections = _sections()
    result, issues = qa_and_repair(_plan(), _qa_tree(), sections, BadClient())
    assert issues  # 残余问题照常上报
    assert result["sec-0001"] is sections["sec-0001"]  # 旧版保留
