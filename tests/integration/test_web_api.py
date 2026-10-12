from __future__ import annotations

import json
import time

import pytest

STEM = "办公技巧商务培训PPT模板"

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
    ],
}, ensure_ascii=False)

# 必拒提案：内容页 2 次调用（拒收→重试→降级）；结构页（cover/toc/2 章节头/
# closing，N2 起 LLM 主路径）2 次调用（拒收→重试→确定性兜底）。3+5 页
FILL_BAD = json.dumps({
    "title": "项目覆盖规模", "slots": {}, "deletes": [], "note": None,
}, ensure_ascii=False)
FILL_CALLS = 16


@pytest.fixture()
def web_app(tmp_path):
    from app.main import create_app

    return create_app(jobs_root=tmp_path / "jobs", com_export=False)


def _wait_status(client, job_id, statuses, timeout=60.0):
    deadline = time.monotonic() + timeout
    d = None
    while time.monotonic() < deadline:
        d = client.get(f"/api/jobs/{job_id}").json()
        if d["status"] in statuses:
            return d
        time.sleep(0.05)
    raise AssertionError(f"等待状态超时：{d['status']}（{d.get('detail')}）")


def test_web_upload_confirm_download(basic_docx, web_app, fake_llm_factory):
    from fastapi.testclient import TestClient

    # 每次 LLMClient() 都换新脚本：understand/plan 阶段与 confirm 后的 fill 阶段各自完整回放
    state = {"script": [SEC1, SEC2, PLAN_LLM]}

    with TestClient(web_app) as client:
        # lifespan 已启动，此刻注入的工厂才会生效（路由按请求时读取）
        web_app.state.llm_client_factory = lambda: fake_llm_factory(state["script"])[0]
        assert client.get("/").status_code == 200

        with open(basic_docx, "rb") as f:
            r = client.post("/api/jobs", files={"file": ("basic.docx", f)},
                            data={"template": STEM})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]

        d = _wait_status(client, job_id, {"PLANNED"})
        assert d["template"] == STEM
        # 确认页 outline 携带模板页指派 + 签名（cover→S1）
        assert d["outline"] and d["outline"][0]["type"] == "cover"
        assert d["outline"][0]["template_slide"] == 1
        assert d["outline"][0].get("template_signature")
        assert all(o["template_slide"] for o in d["outline"])
        assert d["output_pptx"] is None
        assert client.get(f"/jobs/{job_id}").status_code == 200

        state["script"] = [FILL_BAD] * FILL_CALLS
        assert client.post(f"/api/jobs/{job_id}/confirm").status_code == 200
        d = _wait_status(client, job_id, {"DONE", "FAILED"})
        assert d["status"] == "DONE", d.get("error")

        r = client.get(f"/jobs/{job_id}/download")
        assert r.status_code == 200
        assert len(r.content) > 10000

        assert client.post(f"/api/jobs/{job_id}/confirm").status_code == 409


def test_web_upload_rejects_bad_suffix(web_app):
    from fastapi.testclient import TestClient

    with TestClient(web_app) as client:
        r = client.post("/api/jobs", files={"file": ("notes.txt", b"hello")})
        assert r.status_code == 400


def test_web_upload_rejects_unknown_template(basic_docx, web_app):
    from fastapi.testclient import TestClient

    with TestClient(web_app) as client:
        with open(basic_docx, "rb") as f:
            r = client.post("/api/jobs", files={"file": ("basic.docx", f)},
                            data={"template": "no_such_template"})
        assert r.status_code == 400
        assert "no_such_template" in r.json()["detail"]

        with open(basic_docx, "rb") as f:  # PPT 任务缺模板同样 400
            r = client.post("/api/jobs", files={"file": ("basic.docx", f)},
                            data={"template": ""})
        assert r.status_code == 400


def test_web_unknown_job_404(web_app):
    from fastapi.testclient import TestClient

    with TestClient(web_app) as client:
        assert client.get("/api/jobs/nope").status_code == 404
        assert client.get("/jobs/nope").status_code == 404
