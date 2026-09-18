from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates

from app.config import DATA_DIR
from app.parsing.base import ParseError
from app.render.skins import skin_choices
from app.services.jobs import JobError

router = APIRouter()

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

_UPLOAD_SUFFIXES = {".docx", ".pptx", ".ppt", ".pdf"}
_PNG_NAME = re.compile(r"^page_\d{2}\.png$")


def _mgr(request: Request):
    return request.app.state.jobs


def _llm(request: Request):
    factory = getattr(request.app.state, "llm_client_factory", None)
    return factory() if factory else None


# ---- 页面 ----

@router.get("/")
async def index(request: Request):
    jobs = await run_in_threadpool(_mgr(request).list)
    skins = await run_in_threadpool(skin_choices)
    return templates.TemplateResponse(request, "index.html",
                                      {"jobs": jobs[:12], "skins": skins})


@router.get("/jobs/{job_id}")
async def job_page(request: Request, job_id: str):
    try:
        await run_in_threadpool(_mgr(request).get, job_id)
    except JobError as e:
        raise HTTPException(404, str(e)) from e
    skins = dict(await run_in_threadpool(skin_choices))
    return templates.TemplateResponse(request, "job.html",
                                      {"job_id": job_id, "skins": skins})


# ---- API ----

@router.post("/api/jobs")
async def create_job(request: Request, file: UploadFile = File(...),
                     skin: str = Form("business_blue")):
    name = Path(file.filename or "").name
    if Path(name).suffix.lower() not in _UPLOAD_SUFFIXES:
        raise HTTPException(400, "仅支持 docx / pptx / ppt / pdf 上传")
    if skin not in dict(await run_in_threadpool(skin_choices)):
        raise HTTPException(400, f"未知皮肤：{skin}")

    stage = DATA_DIR / "uploads" / uuid.uuid4().hex
    stage.mkdir(parents=True, exist_ok=True)
    staged = stage / name
    staged.write_bytes(await file.read())
    try:
        job = await run_in_threadpool(
            lambda: _mgr(request).create(
                staged, skin=skin, client=_llm(request),
                com_export=getattr(request.app.state, "com_export", True)))
    except ParseError as e:
        raise HTTPException(422, f"解析失败：{e}") from e
    except JobError as e:
        raise HTTPException(409, str(e)) from e
    finally:
        shutil.rmtree(stage, ignore_errors=True)
    return {"job_id": job.job_id}


@router.get("/api/jobs/{job_id}")
async def job_status(request: Request, job_id: str):
    try:
        job = await run_in_threadpool(_mgr(request).get, job_id)
    except JobError as e:
        raise HTTPException(404, str(e)) from e

    d = job.to_dict()

    outline = None
    plan_path = job.dir / "artifacts" / "plan.json"
    if plan_path.exists():
        def _load():
            from app.schema.plan import SlidePlan

            plan = SlidePlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
            return [{"page_no": p.page_no, "type": p.slide_type.value, "title": p.title}
                    for p in plan.pages]

        outline = await run_in_threadpool(_load)

    def _pngs():
        return sorted(p.name for p in (job.dir / "pages").glob("page_*.png"))

    d.update(outline=outline, pages_png=await run_in_threadpool(_pngs))
    return d


@router.post("/api/jobs/{job_id}/confirm")
async def confirm_job(request: Request, job_id: str):
    try:
        await run_in_threadpool(
            lambda: _mgr(request).confirm(
                job_id, client=_llm(request),
                com_export=getattr(request.app.state, "com_export", True)))
    except JobError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@router.get("/jobs/{job_id}/download")
async def download(request: Request, job_id: str):
    try:
        job = await run_in_threadpool(_mgr(request).get, job_id)
    except JobError as e:
        raise HTTPException(404, str(e)) from e
    pptx = job.dir / "artifacts" / "output.pptx"
    if not pptx.exists():
        raise HTTPException(404, "产物尚未生成")
    return FileResponse(pptx, filename=f"{Path(job.source_name).stem}.pptx",
                        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation")


@router.get("/jobs/{job_id}/pages/{name}")
async def page_png(request: Request, job_id: str, name: str):
    if not _PNG_NAME.match(name):
        raise HTTPException(400, "非法文件名")
    try:
        job = await run_in_threadpool(_mgr(request).get, job_id)
    except JobError as e:
        raise HTTPException(404, str(e)) from e
    png = job.dir / "pages" / name
    if not png.exists():
        raise HTTPException(404, "页面尚未导出")
    return FileResponse(png, media_type="image/png")
