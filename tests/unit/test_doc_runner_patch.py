"""通用链路表格布局补丁：应用/收敛/校验/无表；form 分支收敛判停。"""

from __future__ import annotations

from app.llm.prompts import PromptManager
from app.pipeline.doc_runner import _patch_doc_tables
from app.pipeline.form_branch import _visuals_converged
from app.schema.docir import DocIR, DocIRMeta, DocTitleBlock, TableBlock
from app.schema.enums import Genre
from app.schema.tblskeleton import TVisualTable


class _FakeClient:
    vision_model = "fake-vision"

    def __init__(self, reply=None, error: Exception | None = None):
        self._reply = reply
        self._error = error
        self.calls = 0

    def structured(self, schema, messages, stage=None, **kw):
        self.calls += 1
        if self._error:
            raise self._error
        return self._reply


def _doc(col_widths=None, row_heights=None, header=True) -> DocIR:
    t = TableBlock(table_id="t1",
                   header=["姓名", "说明"] if header else [],
                   rows=[["张三", "内容内容内容内容"], ["李四", "内容内容内容内容"]],
                   col_widths=col_widths, row_heights=row_heights)
    return DocIR(meta=DocIRMeta(title="测试", genre=Genre.REPORT),
                 blocks=[DocTitleBlock(text="测试"), t])


# ---- 补丁应用 ----

def test_patch_applies_widths_and_heights():
    doc = _doc(col_widths=[0.2, 0.8], row_heights=[24, 30, 30])
    reply = type("Out", (), {"tables": [TVisualTable(
        table_index=0, col_widths=[30, 70], row_heights=[28, 60, 60])]} )()
    client = _FakeClient(reply)
    assert _patch_doc_tables(doc, ["加宽第1列"], client, PromptManager()) is True
    t = doc.blocks[1]
    assert t.col_widths == [0.30, 0.70] and t.row_heights == [28, 60, 60]


def test_patch_prompt_carries_summary_and_old_params():
    doc = _doc(col_widths=[0.2, 0.8])
    reply = type("Out", (), {"tables": [TVisualTable(
        table_index=0, col_widths=[30, 70])]} )()

    class _Cap(_FakeClient):
        def __init__(self, reply):
            super().__init__(reply)
            self.messages = None

        def structured(self, schema, messages, stage=None, **kw):
            self.messages = messages
            return super().structured(schema, messages, stage, **kw)

    client = _Cap(reply)
    assert _patch_doc_tables(doc, ["加宽第1列（建议：30%）"], client,
                             PromptManager()) is True
    user = client.messages[1]["content"]
    assert "加宽第1列（建议：30%）" in user
    assert '"col_widths": [20, 80]' in user  # 旧参数锚定


def test_patch_converged_returns_false():
    doc = _doc(col_widths=[0.2, 0.8], row_heights=[24, 30, 30])
    reply = type("Out", (), {"tables": [TVisualTable(
        table_index=0, col_widths=[20, 80], row_heights=[24, 30, 30])]} )()
    assert _patch_doc_tables(doc, ["意见"], _FakeClient(reply),
                             PromptManager()) is False
    assert doc.blocks[1].col_widths == [0.2, 0.8]  # 原样


def test_patch_invalid_keeps_old():
    doc = _doc(col_widths=[0.2, 0.8])
    reply = type("Out", (), {"tables": [TVisualTable(
        table_index=0, col_widths=[50, 50, 50])]} )()  # 列数不符
    assert _patch_doc_tables(doc, ["意见"], _FakeClient(reply),
                             PromptManager()) is False
    assert doc.blocks[1].col_widths == [0.2, 0.8]


def test_patch_llm_error_returns_false():
    doc = _doc(col_widths=[0.2, 0.8])
    assert _patch_doc_tables(doc, ["意见"], _FakeClient(error=RuntimeError("x")),
                             PromptManager()) is False


def test_patch_no_tables_returns_false():
    doc = DocIR(meta=DocIRMeta(title="t", genre=Genre.REPORT),
                blocks=[DocTitleBlock(text="t")])
    assert _patch_doc_tables(doc, ["意见"], _FakeClient(),
                             PromptManager()) is False


# ---- form 分支收敛判停 ----

def test_visuals_converged_thresholds():
    old = [TVisualTable(table_index=0, col_widths=[20, 50, 30],
                        row_heights=[24, 60])]
    same = [TVisualTable(table_index=0, col_widths=[20, 50, 30],
                          row_heights=[25, 61])]  # 行高差 <2pt → 收敛
    taller = [TVisualTable(table_index=0, col_widths=[20, 50, 30],
                            row_heights=[24, 62])]  # 行高差 =2pt → 不收敛
    far = [TVisualTable(table_index=0, col_widths=[20, 50, 31],
                         row_heights=[24, 60])]  # 列差 =1 百分点 → 不收敛
    assert _visuals_converged(old, same)
    assert not _visuals_converged(old, taller)
    assert not _visuals_converged(old, far)
    assert not _visuals_converged(old, [])  # 数量不同
