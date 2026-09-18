from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.config import DATA_DIR
from app.parse_dispatch import parse_source
from app.pipeline.runner import parse_and_save, run_pipeline_safe
from app.schema.enums import Genre, JobStatus

_TERMINAL = {JobStatus.DONE, JobStatus.FAILED}
_ACTIVE = {JobStatus.PARSED, JobStatus.UNDERSTOOD, JobStatus.GENERATING,
           JobStatus.RENDERED, JobStatus.QA, JobStatus.REPAIRING, JobStatus.PLANNED}


class JobError(Exception):
    pass


class Job:
    def __init__(self, job_id: str, root: Path, source_name: str, skin: str,
                 auto_confirm: bool = False, product: str = "ppt"):
        self.job_id = job_id
        self.dir = root / job_id
        self.source_name = source_name
        self.skin = skin
        self.product = product
        self.auto_confirm = auto_confirm
        self.confirmed = auto_confirm
        self.status = JobStatus.PARSED
        self.detail = ""
        self.error: str | None = None
        self.created_at = time.strftime("%Y-%m-%dT%H:%M:%S")
        self.updated_at = self.created_at
        self._lock = threading.Lock()

    # ---- 状态 ----

    def set_status(self, status: JobStatus, detail: str = "") -> None:
        with self._lock:
            self.status = status
            self.detail = detail
            self.updated_at = time.strftime("%Y-%m-%dT%H:%M:%S")

    def save_state(self) -> None:
        with self._lock:
            data = {
                "job_id": self.job_id,
                "status": self.status.value,
                "detail": self.detail,
                "source_name": self.source_name,
                "skin": self.skin,
                "product": self.product,
                "auto_confirm": self.auto_confirm,
                "confirmed": self.confirmed,
                "error": self.error,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
            }
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "state.json").write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def to_dict(self) -> dict:
        with self._lock:
            art = self.dir / "artifacts"
            return {
                "job_id": self.job_id,
                "status": self.status.value,
                "detail": self.detail,
                "source_name": self.source_name,
                "skin": self.skin,
                "product": self.product,
                "error": self.error,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "output_pptx": str(art / "output.pptx") if (art / "output.pptx").exists() else None,
                "output_docx": str(art / "output.docx") if (art / "output.docx").exists() else None,
                "output_pdf": str(art / "output.pdf") if (art / "output.pdf").exists() else None,
            }

    @classmethod
    def load(cls, job_dir: Path) -> "Job":
        data = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
        job = cls(data["job_id"], job_dir.parent, data["source_name"],
                  data.get("skin", "business_blue"), data.get("auto_confirm", False),
                  data.get("product", "ppt"))
        job.status = JobStatus(data["status"])
        job.detail = data.get("detail", "")
        job.error = data.get("error")
        job.confirmed = data.get("confirmed", False)
        job.created_at = data.get("created_at", "")
        job.updated_at = data.get("updated_at", "")
        return job

    def refresh_from_disk(self) -> None:
        """别的进程（如 CLI）在驱动该 job 时，同步磁盘状态到内存。"""
        p = self.dir / "state.json"
        if not p.exists():
            return
        data = json.loads(p.read_text(encoding="utf-8"))
        with self._lock:
            self.status = JobStatus(data["status"])
            self.detail = data.get("detail", "")
            self.error = data.get("error")
            self.confirmed = data.get("confirmed", self.confirmed)


class JobManager:
    """进程内单任务管理：全局仅 1 个非终态 job；1-worker 执行器串行跑流水线。"""

    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else DATA_DIR / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._running: set[str] = set()  # 本进程执行器在跑的 job
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="job")
        self._lock = threading.Lock()
        self._scan_existing()

    # ---- 查询 ----

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise JobError(f"任务不存在：{job_id}")
        if job.job_id not in self._running:
            job.refresh_from_disk()  # 可能由其他进程（CLI）驱动
        return job

    def list(self) -> list[dict]:
        self._scan_new()
        for j in self._jobs.values():
            if j.job_id not in self._running:
                j.refresh_from_disk()
        return [j.to_dict() for j in sorted(self._jobs.values(), key=lambda x: x.created_at)]

    def active(self) -> Job | None:
        for j in self._jobs.values():
            if j.job_id not in self._running:
                j.refresh_from_disk()
        return next((j for j in self._jobs.values() if j.status in _ACTIVE), None)

    # ---- 生命周期 ----

    def _submit(self, job: Job, client, com_export: bool) -> None:
        self._running.add(job.job_id)
        fut = self._executor.submit(run_pipeline_safe, job, client, com_export)
        fut.add_done_callback(lambda _f: self._running.discard(job.job_id))

    def create(self, source: Path, skin: str = "business_blue",
               auto_confirm: bool = False, product: str = "ppt",
               client=None, com_export: bool = True) -> Job:
        if self.active() is not None:
            active = self.active()
            raise JobError(f"已有进行中的任务 {active.job_id}（{active.status.value}），"
                           f"请先完成或取消")
        source = Path(source)
        job_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        job = Job(job_id, self.root, source.name, skin, auto_confirm, product)
        job.dir.mkdir(parents=True, exist_ok=True)

        upload = job.dir / "upload"
        upload.mkdir(exist_ok=True)
        target = upload / source.name
        if source.resolve() != target.resolve():
            target.write_bytes(source.read_bytes())
        parse_and_save(target, job)  # 同步解析：失败立刻反馈给调用方
        self._jobs[job_id] = job
        self._submit(job, client, com_export)
        return job

    def confirm(self, job_id: str, client=None, com_export: bool = True,
                genre: str | None = None) -> Job:
        job = self.get(job_id)
        if job.status is not JobStatus.PLANNED:
            raise JobError(f"任务状态为 {job.status.value}，只有 PLANNED 可确认")
        if genre and job.product == "doc":
            self._apply_genre(job, genre)
        job.confirmed = True
        job.save_state()
        self._submit(job, client, com_export)
        return job

    def _apply_genre(self, job: Job, genre: str) -> None:
        """PLANNED 确认时改体裁：只重写 docplan.json 的 genre，不重跑 understand。"""
        try:
            new_genre = Genre(genre)
        except ValueError:
            raise JobError(f"未知体裁：{genre}（可选 {'/'.join(g.value for g in Genre)}）")
        plan_path = job.dir / "artifacts" / "docplan.json"
        if not plan_path.exists():
            return
        data = json.loads(plan_path.read_text(encoding="utf-8"))
        if data.get("genre") != new_genre.value:
            data["genre"] = new_genre.value
            plan_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def cancel(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status in _TERMINAL:
            raise JobError(f"任务已结束（{job.status.value}），无需取消")
        job.error = "用户取消"
        job.set_status(JobStatus.FAILED, "用户取消")
        job.save_state()
        return job

    # ---- 断点续跑 ----

    _RESUME_STALE_S = 300  # state.json 超过该秒数未更新才视为中断（避免误接管别的进程正在跑的 job）

    def _scan_new(self) -> None:
        """list 时发现新增 job 目录（如 CLI 刚创建的），仅纳入列表，不续跑。"""
        for state in self.root.glob("*/state.json"):
            if state.parent.name not in self._jobs:
                try:
                    self._jobs[state.parent.name] = Job.load(state.parent)
                except Exception:
                    continue

    def _scan_existing(self) -> None:
        now = time.time()
        for state in sorted(self.root.glob("*/state.json")):
            try:
                job = Job.load(state.parent)
            except Exception:
                continue
            self._jobs[job.job_id] = job
            if job.status in {JobStatus.UNDERSTOOD, JobStatus.GENERATING,
                              JobStatus.RENDERED, JobStatus.QA, JobStatus.REPAIRING}:
                if now - state.stat().st_mtime < self._RESUME_STALE_S:
                    continue  # 可能仍由其他进程驱动，不接管
                # 中断任务：用户此前已确认过（或 auto），续跑补缺失产物
                job.confirmed = True
                job.save_state()
                self._submit(job, None, True)
