from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import get_settings
from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.parse_dispatch import parse_source
from app.pipeline.fill import fill_deterministic, fill_page
from app.pipeline.plan import assemble_full_plan
from app.pipeline.understand import understand
from app.render.capacity import capacity_issues
from app.render.renderer import render_to_file
from app.schema.doctree import DocTree
from app.schema.docmap import DocMap
from app.schema.enums import JobStatus, SlideType
from app.schema.plan import SlidePlan
from app.schema.slideir import Deck, DeckMeta, SlideIR, validate_deck

log = logging.getLogger(__name__)


class _JobLike:
    """runner 只依赖这些接口，避免与 services.jobs 循环依赖。"""
    dir: Path
    status: JobStatus
    confirmed: bool
    error: str | None

    def set_status(self, status: JobStatus, detail: str = "") -> None: ...
    def save_state(self) -> None: ...


def _artifacts(job: _JobLike) -> Path:
    d = job.dir / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(obj.model_dump_json(indent=2), encoding="utf-8")


def _load_json(path: Path, cls):
    return cls.model_validate_json(path.read_text(encoding="utf-8"))


def toc_entries_of(plan: SlidePlan) -> list[str]:
    entries = [p.title for p in plan.pages if p.slide_type is SlideType.SECTION_HEADER]
    if not entries:
        entries = [p.title for p in plan.pages
                   if p.slide_type not in (SlideType.COVER, SlideType.TOC, SlideType.CLOSING)][:5]
    return entries


def run_pipeline(job: _JobLike, client: LLMClient | None = None,
                 com_export: bool = True) -> None:
    """PARSED→…→DONE 全流程。每阶段先查产物，幂等可续跑。"""
    client = client or LLMClient(log_path=job.dir / "logs" / "llm_calls.jsonl")
    pm = PromptManager()
    art = _artifacts(job)

    # ---- understand ----
    docmap_path = art / "docmap.json"
    if not docmap_path.exists():
        tree = _load_json(art / "doctree.json", DocTree)
        job.set_status(JobStatus.UNDERSTOOD, "docmap")
        job.save_state()
        _save_json(docmap_path, understand(tree, client, pm))
        job.save_state()

    # ---- plan ----
    plan_path = art / "plan.json"
    if not plan_path.exists():
        tree = _load_json(art / "doctree.json", DocTree)
        docmap = _load_json(docmap_path, DocMap)
        job.set_status(JobStatus.UNDERSTOOD, "planning")
        job.save_state()
        content = _plan_content(tree, docmap, client, pm)
        plan = assemble_full_plan(tree, content)
        _save_json(plan_path, plan)
        job.save_state()

    plan = _load_json(plan_path, SlidePlan)
    if not (job.confirmed or getattr(job, "auto_confirm", False)):
        job.set_status(JobStatus.PLANNED, f"{len(plan.pages)} 页待确认")
        job.save_state()
        return

    # ---- fill（逐页落盘，可断点续跑）----
    job.set_status(JobStatus.GENERATING, "fill 0/" + str(len(plan.pages)))
    job.save_state()
    tree = _load_json(art / "doctree.json", DocTree)
    pages_dir = job.dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    toc_entries = toc_entries_of(plan)
    doc_title = plan.title or tree.sections[0].title

    todo = [p for p in plan.pages if not (pages_dir / f"page_{p.page_no:02d}.ir.json").exists()]
    if todo:
        workers = max(1, get_settings().fill_workers)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fill") as ex:
            futs = {}
            for item in todo:
                if item.slide_type in (SlideType.COVER, SlideType.TOC, SlideType.SECTION_HEADER,
                                       SlideType.CLOSING, SlideType.TABLE):
                    futs[ex.submit(fill_deterministic, item, tree, toc_entries, doc_title)] = item
                else:
                    futs[ex.submit(fill_page, item, tree, client, pm)] = item
            done = len(plan.pages) - len(todo)
            for fut in as_completed(futs):
                ir = fut.result()
                _save_json(pages_dir / f"page_{ir.page_no:02d}.ir.json", ir)
                done += 1
                job.set_status(JobStatus.GENERATING, f"fill {done}/{len(plan.pages)}")
                job.save_state()

    slides = [_load_json(pages_dir / f"page_{p.page_no:02d}.ir.json", SlideIR)
              for p in sorted(plan.pages, key=lambda x: x.page_no)]
    deck = Deck(meta=DeckMeta(title=doc_title, template=_skin_of(job)), slides=slides)
    issues = validate_deck(deck)
    if issues:
        raise RuntimeError("SlideIR 未通过校验：\n" + "\n".join(
            f"p{i.page_no} {i.rule.value}: {i.detail}" for i in issues))
    _save_json(art / "slideir.json", deck)

    # ---- render ----
    job.set_status(JobStatus.RENDERED, "render")
    job.save_state()
    pptx = art / "output.pptx"
    render_to_file(deck, _skin_of(job), pptx)

    # ---- QA（W2 范围：容量 + 渲染清单；数字溯源等 W4 接入）----
    job.set_status(JobStatus.QA, "export")
    job.save_state()
    report: list[str] = [f"[capacity] p{q.page_no} {q.code.value}: {q.detail}"
                         for q in capacity_issues(deck)]
    if com_export:
        from app.services.com_export import checklist, export_pngs

        png_dir = job.dir / "pages"
        export_pngs(pptx, png_dir)
        report += [f"[checklist] p{q.page_no} {q.code.value}: {q.detail}"
                   for q in checklist(deck, pptx)]
    (art / "qa_report.txt").write_text("\n".join(report) or "（无问题）", encoding="utf-8")

    job.set_status(JobStatus.DONE, f"{len(slides)} 页")
    job.save_state()


def _plan_content(tree: DocTree, docmap: DocMap, client: LLMClient, pm: PromptManager) -> SlidePlan:
    from app.pipeline.plan import plan_slides

    return plan_slides(tree, docmap, client, pm)


def _skin_of(job: _JobLike) -> str:
    return getattr(job, "skin", "business_blue")


def run_pipeline_safe(job: _JobLike, client: LLMClient | None = None,
                      com_export: bool = True) -> None:
    try:
        run_pipeline(job, client, com_export)
    except Exception as e:  # noqa: BLE001 — 任何阶段异常都落为 FAILED
        log.exception("job %s 失败", getattr(job, "dir", "?"))
        job.error = f"{type(e).__name__}: {e}"
        (job.dir / "logs").mkdir(parents=True, exist_ok=True)
        (job.dir / "logs" / "error.log").write_text(
            f"{job.error}\n\n{traceback.format_exc()}", encoding="utf-8")
        try:
            job.set_status(JobStatus.FAILED, str(e)[:200])
            job.save_state()
        except Exception:
            pass


def parse_and_save(source: Path, job: _JobLike) -> None:
    tree = parse_source(source)
    art = _artifacts(job)
    _save_json(art / "doctree.json", tree)
    job.set_status(JobStatus.PARSED, f"{tree.meta.n_chars} 字 / {len(tree.tables)} 表")
    job.save_state()
