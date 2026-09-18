from __future__ import annotations

import json
import time

import pytest

from app.schema.enums import JobStatus

SEC1 = json.dumps({
    "section_id": "sec-0001",
    "summary": "本章介绍项目覆盖范围与设备规模。",
    "key_facts": [
        {"text": "本项目覆盖 12 个园区，接入设备 8,600 台。", "block_ids": ["blk-0001"]},
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
        {"table_id": "tbl-001", "topic": "Q3 关键指标",
         "headline_cells": [{"row": 1, "col": 2}],
         "suggested_strategy": "copy", "rationale": "3×3 小表"},
    ],
}, ensure_ascii=False)

PLAN_LLM = json.dumps({
    "title": "智慧园区平台建设汇报",
    "pages": [
        {"page_no": 1, "slide_type": "text_points", "title": "项目覆盖规模",
         "brief": "呈现园区与设备规模", "src_refs": ["blk-0001"], "table_ref": None},
        {"page_no": 2, "slide_type": "text_points", "title": "建设进展",
         "brief": "数据中台完成", "src_refs": ["blk-0002"], "table_ref": None},
        {"page_no": 3, "slide_type": "text_points", "title": "达成情况",
         "brief": "总结", "src_refs": ["blk-0005"], "table_ref": None},
        {"page_no": 4, "slide_type": "text_points", "title": "多余页",
         "brief": "引用编造表格", "src_refs": [], "table_ref": {"table_id": "tbl-099", "strategy": "copy"}},
    ],
}, ensure_ascii=False)

FILL_OK = json.dumps({
    "title": "项目覆盖规模",
    "bullets": ["覆盖 12 个园区", "接入设备 8,600 台"], "metrics": [], "note": None,
}, ensure_ascii=False)

# cover + toc + 2 章节头 + 4 内容页 + 1 自动补的表格页 + closing
EXPECTED_PAGES = 10
FILL_CALLS = 4


def _wait(job, statuses, timeout=60.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.status in statuses:
            return job.status
        time.sleep(0.05)
    raise AssertionError(f"等待状态超时：当前 {job.status.value}（{job.detail}）")


def test_auto_confirm_full_flow(basic_docx, fake_llm_factory, tmp_path):
    from app.services.jobs import JobManager

    client, _ = fake_llm_factory([SEC1, SEC2, PLAN_LLM] + [FILL_OK] * FILL_CALLS)
    mgr = JobManager(root=tmp_path / "jobs")
    job = mgr.create(basic_docx, auto_confirm=True, client=client, com_export=False)

    _wait(job, {JobStatus.DONE, JobStatus.FAILED})
    assert job.status is JobStatus.DONE, job.error

    art = job.dir / "artifacts"
    for name in ("doctree.json", "docmap.json", "plan.json", "slideir.json",
                 "output.pptx", "qa_report.txt"):
        assert (art / name).exists(), f"缺少产物 {name}"
    pages = sorted((job.dir / "pages").glob("page_*.ir.json"))
    assert len(pages) == EXPECTED_PAGES
    assert [p.name for p in pages] == [f"page_{i:02d}.ir.json" for i in range(1, EXPECTED_PAGES + 1)]

    # 产物可独立加载（断点续跑的数据基础）
    from app.schema.slideir import Deck

    deck = Deck.model_validate_json((art / "slideir.json").read_text(encoding="utf-8"))
    assert len(deck.slides) == EXPECTED_PAGES
    assert deck.slides[0].slide_type.value == "cover"
    assert deck.slides[-1].slide_type.value == "closing"


def test_planned_checkpoint_then_confirm(basic_docx, fake_llm_factory, tmp_path):
    from app.services.jobs import JobError, JobManager

    client1, _ = fake_llm_factory([SEC1, SEC2, PLAN_LLM])
    mgr = JobManager(root=tmp_path / "jobs")
    job = mgr.create(basic_docx, client=client1, com_export=False)

    _wait(job, {JobStatus.PLANNED})
    assert (job.dir / "artifacts" / "plan.json").exists()
    assert not (job.dir / "artifacts" / "slideir.json").exists()

    with pytest.raises(JobError):  # 全局仅 1 活跃任务
        mgr.create(basic_docx, client=client1, com_export=False)

    client2, fake2 = fake_llm_factory([FILL_OK] * FILL_CALLS)
    mgr.confirm(job.job_id, client=client2, com_export=False)
    _wait(job, {JobStatus.DONE, JobStatus.FAILED})
    assert job.status is JobStatus.DONE, job.error
    # 断点续跑：docmap/plan 产物已存在，第二个 client 只被 fill 调用
    assert len(fake2.calls) == FILL_CALLS


def test_cancel_frees_slot(basic_docx, fake_llm_factory, tmp_path):
    from app.services.jobs import JobError, JobManager

    client, _ = fake_llm_factory([SEC1, SEC2, PLAN_LLM])
    mgr = JobManager(root=tmp_path / "jobs")
    job = mgr.create(basic_docx, client=client, com_export=False)
    _wait(job, {JobStatus.PLANNED})

    mgr.cancel(job.job_id)
    assert job.status is JobStatus.FAILED
    assert job.error == "用户取消"
    with pytest.raises(JobError):  # 已终态，不能重复取消
        mgr.cancel(job.job_id)

    client2, _ = fake_llm_factory([SEC1, SEC2, PLAN_LLM])
    job2 = mgr.create(basic_docx, client=client2, com_export=False)
    _wait(job2, {JobStatus.PLANNED})
