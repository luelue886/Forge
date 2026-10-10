"""M6/N2 PPT 全流程集成：真模板目录 → plan 指派 → slotfill（降级链）→ build_deck → QA。

fill 阶段用"必拒"脚本走满 校验→重试→降级/兜底 链：内容页每页恰好 2 次
LLM 调用后降级（title-only + 源文备注）；结构页（N2 起 LLM 主路径）每页
恰好 2 次调用后兜底确定性填充。两者都是合法产物——管线必须走完 DONE。
"""
from __future__ import annotations

import json
import time

import pytest

from app.schema.enums import JobStatus

STEM = "办公技巧商务培训PPT模板"  # 33 页中文模板：真模板全链（零 COM）

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

# 空槽提案：必触发 V-SLOT-COVER → 1 轮重试仍拒 → 降级（title-only + 源文备注）
FILL_BAD = json.dumps({
    "title": "项目覆盖规模", "slots": {}, "deletes": [], "note": None,
}, ensure_ascii=False)

# cover + toc + 2 章节头 + 4 内容页 + 1 自动补的表格页 + closing
EXPECTED_PAGES = 10
CONTENT_PAGES = 4
STRUCTURAL_PAGES = 5  # cover + toc + 2 section + closing（表格页零 LLM）
# 每页恰好 2 次提案（拒收→重试→内容页降级 / 结构页确定性兜底）
FILL_CALLS = (CONTENT_PAGES + STRUCTURAL_PAGES) * 2


def _wait(job, statuses, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job.status in statuses:
            return job.status
        time.sleep(0.05)
    raise AssertionError(f"等待状态超时：当前 {job.status.value}（{job.detail}）")


def _assert_done(job):
    assert job.status is JobStatus.DONE, job.error

    art = job.dir / "artifacts"
    for name in ("doctree.json", "docmap.json", "plan.json", "deckbuild.json",
                 "output.pptx", "qa_report.txt"):
        assert (art / name).exists(), f"缺少产物 {name}"
    pages = sorted((job.dir / "pages").glob("page_*.fill.json"))
    assert [p.name for p in pages] == [
        f"page_{i:02d}.fill.json" for i in range(1, EXPECTED_PAGES + 1)]

    from app.schema.slotfill import DeckBuild

    deck = DeckBuild.model_validate_json(
        (art / "deckbuild.json").read_text(encoding="utf-8"))
    assert deck.meta.template == STEM
    assert len(deck.pages) == EXPECTED_PAGES
    assert [p.page_no for p in deck.pages] == list(range(1, EXPECTED_PAGES + 1))
    assert all(1 <= p.template_slide <= 33 for p in deck.pages)
    # 降级页也是合法填充：title 槽有值
    for p in deck.pages:
        if p.slot_values:
            assert all(v.strip() for v in p.slot_values.values()), p.page_no
    # QA 报告无 blocking 清单项（页数/遗留/空页全过）
    qa = (art / "qa_report.txt").read_text(encoding="utf-8")
    assert qa == "（无问题）", qa


def test_auto_confirm_full_flow(basic_docx, fake_llm_factory, tmp_path):
    from app.services.jobs import JobManager

    client, _ = fake_llm_factory([SEC1, SEC2, PLAN_LLM] + [FILL_BAD] * FILL_CALLS)
    mgr = JobManager(root=tmp_path / "jobs")
    job = mgr.create(basic_docx, template=STEM, auto_confirm=True,
                     client=client, com_export=False)

    _wait(job, {JobStatus.DONE, JobStatus.FAILED})
    _assert_done(job)


def test_planned_checkpoint_then_confirm(basic_docx, fake_llm_factory, tmp_path):
    from app.services.jobs import JobError, JobManager

    client1, _ = fake_llm_factory([SEC1, SEC2, PLAN_LLM])
    mgr = JobManager(root=tmp_path / "jobs")
    job = mgr.create(basic_docx, template=STEM, client=client1, com_export=False)

    _wait(job, {JobStatus.PLANNED})
    assert (job.dir / "artifacts" / "plan.json").exists()
    assert not (job.dir / "artifacts" / "deckbuild.json").exists()

    # 确认前可见每页的模板页指派
    from app.schema.plan import SlidePlan

    plan = SlidePlan.model_validate_json(
        (job.dir / "artifacts" / "plan.json").read_text(encoding="utf-8"))
    assert all(p.template_slide is not None for p in plan.pages)
    assert plan.pages[0].template_slide == 1  # cover→S1

    with pytest.raises(JobError):  # 全局仅 1 活跃任务
        mgr.create(basic_docx, template=STEM, client=client1, com_export=False)

    client2, fake2 = fake_llm_factory([FILL_BAD] * FILL_CALLS)
    mgr.confirm(job.job_id, client=client2, com_export=False)
    _wait(job, {JobStatus.DONE, JobStatus.FAILED})
    _assert_done(job)
    # 断点续跑：docmap/plan 产物已存在，第二个 client 只被 fill 调用（每页 2 次拒收提案）
    assert len(fake2.calls) == FILL_CALLS


def test_cancel_frees_slot(basic_docx, fake_llm_factory, tmp_path):
    from app.services.jobs import JobError, JobManager

    client, _ = fake_llm_factory([SEC1, SEC2, PLAN_LLM])
    mgr = JobManager(root=tmp_path / "jobs")
    job = mgr.create(basic_docx, template=STEM, client=client, com_export=False)
    _wait(job, {JobStatus.PLANNED})

    mgr.cancel(job.job_id)
    assert job.status is JobStatus.CANCELLED
    # A2：取消是幂等的（重复取消不报错）；错误字段不设置（取消非失败）
    mgr.cancel(job.job_id)
    assert job.error is None

    client2, _ = fake_llm_factory([SEC1, SEC2, PLAN_LLM])
    job2 = mgr.create(basic_docx, template=STEM, client=client2, com_export=False)
    _wait(job2, {JobStatus.PLANNED})


def test_missing_template_rejected_at_create(basic_docx, tmp_path):
    """PPT 任务不带 template：创建即拒（不再进入管线后失败）。"""
    from app.services.jobs import JobError, JobManager

    mgr = JobManager(root=tmp_path / "jobs")
    with pytest.raises(JobError, match="必须指定模板"):
        mgr.create(basic_docx, auto_confirm=True, com_export=False)
    with pytest.raises(JobError, match="未知模板"):
        mgr.create(basic_docx, template="no_such_template",
                   auto_confirm=True, com_export=False)
    assert mgr.active() is None  # 未占用执行槽
