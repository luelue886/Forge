from __future__ import annotations

import pytest

from app.schema.docir import (
    ClosingBlock,
    DocIR,
    DocIRMeta,
    DocTitleBlock,
    HeadingBlock,
    ParaBlock,
    SalutationBlock,
    SectionIR,
    SignatureBlock,
    TableBlock,
    validate_docir,
)
from app.schema.enums import Genre, IssueCode


def _letter_doc() -> DocIR:
    return DocIR(
        meta=DocIRMeta(title="关于调岗的申请书", genre=Genre.LETTER),
        blocks=[
            DocTitleBlock(text="关于调岗的申请书"),
            SalutationBlock(text="尊敬的公司领导："),
            ParaBlock(text="您好！我于 2023 年 3 月入职现岗位，现申请调至市场部。"),
            ParaBlock(text="恳请领导批准。"),
            ClosingBlock(lines=["此致", "敬礼！"]),
            SignatureBlock(signer="申请人：王明", date="2026 年 9 月 18 日"),
        ],
    )


def _report_doc() -> DocIR:
    return DocIR(
        meta=DocIRMeta(title="2026 年第三季度运营报告", genre=Genre.REPORT),
        blocks=[
            DocTitleBlock(text="2026 年第三季度运营报告"),
            HeadingBlock(level=1, text="一、总体情况"),
            ParaBlock(text="本季度营收 1,234.56 万元，同比增长 18%。"),
            HeadingBlock(level=2, text="（一）渠道结构"),
            ParaBlock(text="直销占比 43.8%，渠道占比 56.2%。"),
            TableBlock(table_id="t1", header=["渠道", "占比"], rows=[["直销", "43.8%"]]),
        ],
    )


def _form_doc() -> DocIR:
    return DocIR(
        meta=DocIRMeta(title="员工信息登记表", genre=Genre.FORM),
        blocks=[
            DocTitleBlock(text="员工信息登记表"),
            ParaBlock(text="请如实填写以下信息，本表仅用于人事备案。"),
            TableBlock(table_id="t1", header=["姓名", "部门"], rows=[["王明", "市场部"]]),
        ],
    )


def _rules(doc: DocIR) -> set[IssueCode]:
    return {i.rule for i in validate_docir(doc)}


def test_valid_letter_has_no_issues():
    assert validate_docir(_letter_doc()) == []


def test_valid_report_has_no_issues():
    assert validate_docir(_report_doc()) == []


def test_valid_form_has_no_issues():
    assert validate_docir(_form_doc()) == []


def test_title_length_boundary():
    doc = _report_doc()
    doc.blocks[0] = DocTitleBlock(text="汉" * 30)
    assert IssueCode.V_DOC_TITLE_LEN not in _rules(doc)
    doc.blocks[0] = DocTitleBlock(text="汉" * 31)
    assert IssueCode.V_DOC_TITLE_LEN in _rules(doc)


def test_heading_length_boundary():
    doc = _report_doc()
    doc.blocks[1] = HeadingBlock(level=1, text="汉" * 25)
    assert IssueCode.V_DOC_HEADING_LEN not in _rules(doc)
    doc.blocks[1] = HeadingBlock(level=1, text="汉" * 26)
    assert IssueCode.V_DOC_HEADING_LEN in _rules(doc)


def test_genre_block_whitelist():
    doc = _letter_doc()
    doc.blocks.insert(2, HeadingBlock(level=1, text="一、申请事由"))
    assert IssueCode.V_DOC_GENRE_BLOCKS in _rules(doc)

    doc = _report_doc()
    doc.blocks.insert(1, SalutationBlock(text="尊敬的领导："))
    assert IssueCode.V_DOC_GENRE_BLOCKS in _rules(doc)


def test_doc_title_must_be_unique_and_first():
    doc = _report_doc()
    doc.blocks.append(DocTitleBlock(text="附录"))
    rules = _rules(doc)
    assert IssueCode.V_DOC_STRUCTURE in rules

    doc = _report_doc()
    doc.blocks.insert(0, ParaBlock(text="前置段落"))
    assert IssueCode.V_DOC_STRUCTURE in _rules(doc)


def test_letter_frame_order():
    # salutation 不紧跟 doc_title
    doc = _letter_doc()
    doc.blocks.insert(1, ParaBlock(text="开头段"))
    assert IssueCode.V_DOC_STRUCTURE in _rules(doc)

    # signature 在 closing 之前
    doc = _letter_doc()
    sig, clo = doc.blocks[5], doc.blocks[4]
    doc.blocks[4], doc.blocks[5] = sig, clo
    assert IssueCode.V_DOC_STRUCTURE in _rules(doc)

    # signature 夹在正文中间
    doc = _letter_doc()
    doc.blocks.insert(3, SignatureBlock(signer="王明", date=""))
    assert IssueCode.V_DOC_STRUCTURE in _rules(doc)


def test_heading_no_level_skip():
    doc = _report_doc()
    doc.blocks[1] = HeadingBlock(level=2, text="（一）渠道")  # 首个标题即二级
    assert IssueCode.V_DOC_STRUCTURE in _rules(doc)

    doc = _report_doc()
    doc.blocks[1] = HeadingBlock(level=3, text="一、总体")
    assert IssueCode.V_DOC_STRUCTURE in _rules(doc)


def test_form_requires_table_and_caps_paras():
    doc = _form_doc()
    doc.blocks = [doc.blocks[0], doc.blocks[1]]  # 去掉表格
    assert IssueCode.V_DOC_STRUCTURE in _rules(doc)

    doc = _form_doc()
    for i in range(7):
        doc.blocks.insert(1, ParaBlock(text=f"说明段 {i}。"))
    assert IssueCode.V_DOC_STRUCTURE in _rules(doc)


def test_empty_blocks():
    doc = _report_doc()
    doc.blocks.append(ParaBlock(text="   "))
    doc.blocks.append(TableBlock(table_id="t9", header=[], rows=[]))
    assert sum(1 for i in validate_docir(doc) if i.rule is IssueCode.V_DOC_EMPTY) == 2


def test_para_long_is_warning_not_blocking():
    doc = _report_doc()
    doc.blocks.insert(2, ParaBlock(text="汉" * 401))
    issues = validate_docir(doc)
    assert IssueCode.W_DOC_PARA_LONG in {i.rule for i in issues}
    assert all(i.rule is not IssueCode.V_DOC_STRUCTURE for i in issues)


def test_section_ir_roundtrip():
    sec = SectionIR(section_id="s2", blocks=[
        HeadingBlock(level=1, text="二、风险分析"),
        ParaBlock(text="主要风险有三。"),
        TableBlock(table_id="t1", header=["风险", "等级"], rows=[["回款", "中"]]),
    ])
    data = sec.model_dump_json()
    back = SectionIR.model_validate_json(data)
    assert isinstance(back.blocks[0], HeadingBlock)
    assert isinstance(back.blocks[2], TableBlock)
    assert back.blocks[2].rows == [["回款", "中"]]


def test_docir_rejects_unknown_block_kind():
    with pytest.raises(Exception):
        DocIR.model_validate({
            "meta": {"title": "t", "genre": "report"},
            "blocks": [{"kind": "nope", "text": "x"}],
        })
