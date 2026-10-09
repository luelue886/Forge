from __future__ import annotations

import types
from pathlib import Path

import pytest


class FakeCompletions:
    """按脚本回放的假 chat.completions。元素为 str（正常回复）或 Exception。"""

    def __init__(self, script: list):
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        msg = types.SimpleNamespace(content=item)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


class FakeAPI:
    def __init__(self, script: list):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(script))


@pytest.fixture(scope="session")
def basic_docx(tmp_path_factory) -> Path:
    from docx import Document

    doc = Document()
    doc.add_heading("智慧园区平台建设汇报", 0)
    doc.add_heading("一、项目概述", level=1)
    doc.add_paragraph("本项目覆盖 12 个园区，接入设备 8,600 台。")
    doc.add_paragraph("首期建设要点：", style="List Bullet")
    doc.add_paragraph("数据中台已完成", style="List Bullet")
    doc.add_heading("二、季度指标", level=1)
    doc.add_paragraph("表1：Q3 关键指标")
    t = doc.add_table(rows=3, cols=3)
    for j, h in enumerate(["指标", "目标", "实际"]):
        t.rows[0].cells[j].text = h
    for i, row in enumerate([["园区数", "10", "12"], ["设备数", "8000", "8600"]]):
        for j, v in enumerate(row):
            t.rows[i + 1].cells[j].text = v
    doc.add_paragraph("综上，季度目标整体达成率 120%。")
    p = tmp_path_factory.mktemp("docx") / "basic.docx"
    doc.save(str(p))
    return p


@pytest.fixture(scope="session")
def fake_llm_factory(tmp_path_factory):
    """返回 (client, fake) 工厂，脚本耗尽时立即报错而非静默。"""
    from app.llm.client import LLMClient

    log = tmp_path_factory.mktemp("llm") / "calls.jsonl"

    def make(script: list):
        api = FakeAPI(script)
        client = LLMClient(log_path=log, api=api)  # type: ignore[arg-type]
        return client, api.chat.completions

    return make
