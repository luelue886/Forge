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
_DOC_UPLOAD_SUFFIXES = {".docx", ".pdf"}  # 文档线源：PPT 源 → Word 本轮不做
_PNG_NAME = re.compile(r"^page_\d{2}\.png$")

_MEDIA_TYPES = {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
}


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
                     skin: str = Form("business_blue"),
                     product: str = Form("ppt")):
    name = Path(file.filename or "").name
    suffix = Path(name).suffix.lower()
    if product not in ("ppt", "doc"):
        raise HTTPException(400, "product 仅支持 ppt / doc")
    allowed = _DOC_UPLOAD_SUFFIXES if product == "doc" else _UPLOAD_SUFFIXES
    if suffix not in allowed:
        wanted = "docx / pdf" if product == "doc" else "docx / pptx / ppt / pdf"
        raise HTTPException(400, f"仅支持 {wanted} 上传")
    if skin not in dict(await run_in_threadpool(skin_choices)):
        raise HTTPException(400, f"未知皮肤：{skin}")

    stage = DATA_DIR / "uploads" / uuid.uuid4().hex
    stage.mkdir(parents=True, exist_ok=True)
    staged = stage / name
    staged.write_bytes(await file.read())
    try:
        job = await run_in_threadpool(
            lambda: _mgr(request).create(
                staged, skin=skin, product=product, client=_llm(request),
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

    if job.product == "doc":
        def _load_doc():
            from app.schema.docplan import DocPlan

            plan = DocPlan.model_validate_json(plan_doc_path.read_text(encoding="utf-8"))
            kinds = {0: "prelude", 1: "h1", 2: "h2"}
            return {
                "genre": plan.genre.value,
                "title": plan.title,
                "outline": [{"no": it.seq, "type": kinds.get(it.heading_level, "h2"),
                             "title": it.heading} for it in plan.items],
            }

        plan_doc_path = job.dir / "artifacts" / "docplan.json"
        if plan_doc_path.exists():
            d.update(await run_in_threadpool(_load_doc))
    else:
        def _load():
            from app.schema.plan import SlidePlan

            plan = SlidePlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
            return [{"page_no": p.page_no, "type": p.slide_type.value, "title": p.title}
                    for p in plan.pages]

        plan_path = job.dir / "artifacts" / "plan.json"
        if plan_path.exists():
            d["outline"] = await run_in_threadpool(_load)

    def _pngs():
        return sorted(p.name for p in (job.dir / "pages").glob("page_*.png"))

    d["pages_png"] = await run_in_threadpool(_pngs)
    return d


@router.post("/api/jobs/{job_id}/confirm")
async def confirm_job(request: Request, job_id: str,
                      genre: str = Form(None)):
    try:
        await run_in_threadpool(
            lambda: _mgr(request).confirm(
                job_id, client=_llm(request),
                com_export=getattr(request.app.state, "com_export", True),
                genre=genre or None))
    except JobError as e:
        raise HTTPException(409, str(e)) from e
    return {"ok": True}


@router.get("/jobs/{job_id}/download")
async def download(request: Request, job_id: str, format: str = "pptx"):
    try:
        job = await run_in_threadpool(_mgr(request).get, job_id)
    except JobError as e:
        raise HTTPException(404, str(e)) from e
    fmt = format.lower()
    if job.product == "doc":
        if fmt not in ("docx", "pdf"):
            raise HTTPException(400, "文档任务仅支持 format=docx / pdf")
    elif fmt != "pptx":
        raise HTTPException(400, "PPT 任务仅支持 format=pptx")
    f = job.dir / "artifacts" / f"output.{fmt}"
    if not f.exists():
        raise HTTPException(404, "产物尚未生成")
    return FileResponse(f, filename=f"{Path(job.source_name).stem}.{fmt}",
                        media_type=_MEDIA_TYPES[f".{fmt}"])


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
