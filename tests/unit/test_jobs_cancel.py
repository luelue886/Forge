"""A2 协作式取消：CANCELLED 状态、检查点、恢复排除、Web/CLI 接线。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.schema.enums import JobStatus
from app.services.jobs import Job, JobCancelled, JobError, JobManager
from app.pipeline.runner import run_pipeline_safe


# ---- Job 原语 ----

def test_cancel_event_primitives(tmp_path: Path):
    job = Job("j1", tmp_path, "s.docx", "business_blue", False, "doc")
    job.check_cancel()  # 未取消：无操作
    job.request_cancel()
    with __import__("pytest").raises(JobCancelled):
        job.check_cancel()


# ---- JobManager.cancel ----

def test_cancel_planned_job_status_cancelled(tmp_path: Path):
    mgr = JobManager(root=tmp_path)
    job = Job("j1", tmp_path, "s.docx", "business_blue", False, "doc")
    job.status = JobStatus.PLANNED
    mgr._jobs["j1"] = job
    out = mgr.cancel("j1")
    assert out.status is JobStatus.CANCELLED
    assert json.loads((job.dir / "state.json").read_text(encoding="utf-8"))["status"] == "CANCELLED"


def test_cancel_idempotent_and_terminal_rejected(tmp_path: Path):
    mgr = JobManager(root=tmp_path)
    job = Job("j1", tmp_path, "s.docx", "business_blue", False, "doc")
    job.status = JobStatus.GENERATING
    mgr._jobs["j1"] = job
    mgr.cancel("j1")
    again = mgr.cancel("j1")  # 已 CANCELLED：幂等返回
    assert again.status is JobStatus.CANCELLED

    done = Job("j2", tmp_path, "s.docx", "business_blue", False, "doc")
    done.status = JobStatus.DONE
    mgr._jobs["j2"] = done
    with __import__("pytest").raises(JobError, match="已结束"):
        mgr.cancel("j2")


def test_cancelled_not_in_active_and_excluded_from_resume(tmp_path: Path):
    # CANCELLED 不在 _ACTIVE：active() 找不到 → 新任务可创建
    mgr = JobManager(root=tmp_path)
    job = Job("j1", tmp_path, "s.docx", "business_blue", False, "doc")
    job.status = JobStatus.GENERATING
    mgr._jobs["j1"] = job
    mgr.cancel("j1")
    assert mgr.active() is None

    # 重启扫描（_scan_existing）不复活 CANCELLED（不在恢复集）：
    mgr2 = JobManager(root=tmp_path)
    loaded = mgr2._jobs["j1"]
    assert loaded.status is JobStatus.CANCELLED
    assert "j1" not in mgr2._running  # 未被提交续跑


# ---- run_pipeline_safe 捕获 JobCancelled ----

class _CancelJob:
    """最小 job 桩：check_cancel 在第一次 set_status 后抛 JobCancelled。"""

    def __init__(self, root: Path):
        self.dir = root / "j1"
        self.status = JobStatus.PARSED
        self.product = "doc"
        self.confirmed = True
        self.error = None
        self._fired = False

    def set_status(self, status, detail=""):
        self.status = status
        if not self._fired and status is JobStatus.UNDERSTOOD:
            self._fired = True  # 模拟 docplan 阶段收到取消请求
            raise JobCancelled("用户取消")

    def save_state(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "state.json").write_text(
            json.dumps({"status": self.status.value}), encoding="utf-8")

    def check_cancel(self):
        if self._fired:
            raise JobCancelled("用户取消")


def test_run_pipeline_safe_stops_at_checkpoint(tmp_path: Path):
    job = _CancelJob(tmp_path)
    # doc_runner 会在 docplan 前检查取消：桩在 set_status(UNDERSTOOD) 时置位
    job._fired = True
    run_pipeline_safe(job, client=None, com_export=False)
    assert job.status is JobStatus.CANCELLED
    assert json.loads((job.dir / "state.json").read_text(encoding="utf-8"))["status"] == "CANCELLED"
    assert not (job.dir / "logs" / "error.log").exists()  # 取消不是失败


def test_run_pipeline_safe_check_cancel_raised_via_pipeline(tmp_path: Path):
    # 直接从 _check_cancel 路径验证：doc_runner 的 docplan 检查点捕获
    job = _CancelJob(tmp_path)
    job._fired = True
    from app.pipeline.doc_runner import run_doc_pipeline
    try:
        run_doc_pipeline(job, client=None, com_export=False)
        raise AssertionError("应抛 JobCancelled")
    except JobCancelled:
        pass
    # run_pipeline_safe 是正常出口：状态定格 CANCELLED
    job2 = _CancelJob(tmp_path)
    job2._fired = True
    run_pipeline_safe(job2, client=None, com_export=False)
    assert job2.status is JobStatus.CANCELLED


# ---- Web 路由 ----

def test_web_cancel_endpoint(tmp_path: Path):
    from fastapi.testclient import TestClient

    from app.main import app as fastapp

    mgr = JobManager(root=tmp_path)
    fastapp.state.jobs = mgr
    job = Job("j1", tmp_path, "s.docx", "business_blue", False, "doc")
    job.status = JobStatus.PLANNED
    mgr._jobs["j1"] = job

    client = TestClient(fastapp)
    r = client.post("/api/jobs/j1/cancel")
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    # 状态接口同步可见
    assert client.get("/api/jobs/j1").json()["status"] == "CANCELLED"


def test_web_cancel_terminal_409(tmp_path: Path):
    from fastapi.testclient import TestClient

    from app.main import app as fastapp

    mgr = JobManager(root=tmp_path)
    fastapp.state.jobs = mgr
    job = Job("j1", tmp_path, "s.docx", "business_blue", False, "doc")
    job.status = JobStatus.DONE
    mgr._jobs["j1"] = job

    client = TestClient(fastapp)
    assert client.post("/api/jobs/j1/cancel").status_code == 409


# ---- CLI ----

def test_cli_cancel_subcommand(tmp_path: Path, capsys, monkeypatch):
    from app.cli import main

    import app.services.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "DATA_DIR", tmp_path)
    job = Job("j1", tmp_path / "jobs", "s.docx", "business_blue", False, "doc")
    job.status = JobStatus.GENERATING
    job.save_state()

    rc = main(["cancel", "j1"])
    out = capsys.readouterr().out
    assert rc == 0 and "取消" in out
    # 落盘校验
    data = json.loads((tmp_path / "jobs" / "j1" / "state.json").read_text(encoding="utf-8"))
    assert data["status"] == "CANCELLED"
