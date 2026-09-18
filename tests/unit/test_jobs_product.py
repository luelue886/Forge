from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.schema.enums import Genre, JobStatus
from app.services.jobs import Job, JobError, JobManager


def test_job_product_roundtrip(tmp_path: Path):
    job = Job("j1", tmp_path, "src.docx", "business_blue", False, "doc")
    job.save_state()
    assert json.loads((tmp_path / "j1" / "state.json").read_text(encoding="utf-8"))["product"] == "doc"

    loaded = Job.load(tmp_path / "j1")
    assert loaded.product == "doc"

    d = job.to_dict()
    assert d["product"] == "doc"
    assert d["output_docx"] is None and d["output_pdf"] is None

    art = job.dir / "artifacts"
    art.mkdir(parents=True)
    (art / "output.docx").write_bytes(b"x")
    (art / "output.pdf").write_bytes(b"x")
    d = job.to_dict()
    assert d["output_docx"].endswith("output.docx")
    assert d["output_pdf"].endswith("output.pdf")


def test_job_load_defaults_to_ppt(tmp_path: Path):
    job = Job("j2", tmp_path, "s.docx", "business_blue")
    job.save_state()
    p = tmp_path / "j2" / "state.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    del data["product"]  # 模拟旧版 state.json
    p.write_text(json.dumps(data), encoding="utf-8")
    assert Job.load(tmp_path / "j2").product == "ppt"


def _planned_doc_job(mgr: JobManager, root: Path, genre: str = "letter") -> Job:
    job = Job("j1", root, "src.docx", "business_blue", False, "doc")
    job.status = JobStatus.PLANNED
    mgr._jobs["j1"] = job
    plan_path = job.dir / "artifacts" / "docplan.json"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(json.dumps(
        {"genre": genre, "title": "调岗申请书", "items": []}, ensure_ascii=False),
        encoding="utf-8")
    return job


def test_apply_genre_rewrites_docplan(tmp_path: Path):
    mgr = JobManager(root=tmp_path)
    job = _planned_doc_job(mgr, tmp_path)
    plan_path = job.dir / "artifacts" / "docplan.json"

    mgr._apply_genre(job, "report")
    data = json.loads(plan_path.read_text(encoding="utf-8"))
    assert data["genre"] == Genre.REPORT.value
    assert data["title"] == "调岗申请书"  # 其余字段不动

    with pytest.raises(JobError, match="未知体裁"):
        mgr._apply_genre(job, "bogus")


def test_apply_genre_without_docplan_is_noop(tmp_path: Path):
    mgr = JobManager(root=tmp_path)
    job = Job("j1", tmp_path, "src.docx", "business_blue", False, "doc")
    mgr._apply_genre(job, "report")  # docplan 尚未生成：静默跳过
    assert not (job.dir / "artifacts" / "docplan.json").exists()


def test_confirm_rejects_unknown_genre_before_submit(tmp_path: Path):
    mgr = JobManager(root=tmp_path)
    _planned_doc_job(mgr, tmp_path)
    with pytest.raises(JobError, match="未知体裁"):
        mgr.confirm("j1", genre="bogus", com_export=False)
    assert mgr._jobs["j1"].confirmed is False  # 未确认、未提交


def test_cli_render_docir(tmp_path: Path):
    from app.cli import main
    from app.schema.docir import DocIR, DocIRMeta, DocTitleBlock, ParaBlock

    doc = DocIR(meta=DocIRMeta(title="测试文档", genre=Genre.REPORT), blocks=[
        DocTitleBlock(text="测试文档"),
        ParaBlock(text="第一段正文内容。"),
    ])
    deck_path = tmp_path / "docir.json"
    deck_path.write_text(doc.model_dump_json(), encoding="utf-8")
    out = tmp_path / "out.docx"

    rc = main(["render", str(deck_path), "--docir", "--out", str(out)])
    assert rc == 0
    assert out.exists() and out.stat().st_size > 1000
