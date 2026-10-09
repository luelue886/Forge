from __future__ import annotations

import logging
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.config import get_settings
from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.parse_dispatch import parse_source
from app.pipeline.plan import assemble_full_plan, assign_template_slides
from app.pipeline.slotfill import slotfill_deterministic, slotfill_page
from app.pipeline.understand import understand
from app.render.template_build import build_deck
from app.render.tpl_catalog import TemplateCatalog, load_catalog, template_pptx_path
from app.schema.doctree import DocTree
from app.schema.docmap import DocMap
from app.schema.enums import JobStatus, SlideType
from app.schema.plan import SlidePlan
from app.schema.slotfill import BuildMeta, DeckBuild, PageFill, validate_deck_build

log = logging.getLogger(__name__)


class _JobLike:
    """runner 只依赖这些接口，避免与 services.jobs 循环依赖。

    check_cancel（协作式取消检查点）由 services.Job 提供；CLI 等轻量
    驱动方不带该属性时跳过（getattr 防御，测试桩可省）。
    """
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
    """PARSED→…→DONE 全流程。每阶段先查产物，幂等可续跑。

    各阶段边界 + fill 逐页间隙检查协作式取消标志（A2）。
    """
    client = client or LLMClient(log_path=job.dir / "logs" / "llm_calls.jsonl")
    pm = PromptManager()
    art = _artifacts(job)
    catalog = _catalog_of(job)  # PPT 线必须带模板目录（真模板填充链）

    # ---- understand ----
    docmap_path = art / "docmap.json"
    if not docmap_path.exists():
        _check_cancel(job)
        tree = _load_json(art / "doctree.json", DocTree)
        job.set_status(JobStatus.UNDERSTOOD, "docmap")
        job.save_state()
        _save_json(docmap_path, understand(tree, client, pm))
        job.save_state()

    # ---- plan ----
    plan_path = art / "plan.json"
    if not plan_path.exists():
        _check_cancel(job)
        tree = _load_json(art / "doctree.json", DocTree)
        docmap = _load_json(docmap_path, DocMap)
        job.set_status(JobStatus.UNDERSTOOD, "planning")
        job.save_state()
        content = _plan_content(tree, docmap, client, pm, catalog)
        plan = assemble_full_plan(tree, content, catalog)
        assign_template_slides(plan, catalog)
        _save_json(plan_path, plan)
        job.save_state()

    plan = _load_json(plan_path, SlidePlan)
    if not (job.confirmed or getattr(job, "auto_confirm", False)):
        job.set_status(JobStatus.PLANNED, f"{len(plan.pages)} 页待确认")
        job.save_state()
        return

    unassigned = [p.page_no for p in plan.pages if p.template_slide is None]
    if unassigned:
        raise RuntimeError(f"页 {unassigned} 未指派模板页（template_slide 缺失）")

    # ---- fill（逐页落盘，可断点续跑）----
    job.set_status(JobStatus.GENERATING, "fill 0/" + str(len(plan.pages)))
    job.save_state()
    tree = _load_json(art / "doctree.json", DocTree)
    pages_dir = job.dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    toc_entries = toc_entries_of(plan)
    doc_title = plan.title or tree.sections[0].title

    todo = [p for p in plan.pages
            if not (pages_dir / f"page_{p.page_no:02d}.fill.json").exists()]
    if todo:
        workers = max(1, get_settings().fill_workers)
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="fill") as ex:
            futs = {}
            for item in todo:
                cat_slide = catalog.slide(item.template_slide)  # type: ignore[arg-type]
                if item.slide_type in (SlideType.COVER, SlideType.TOC, SlideType.SECTION_HEADER,
                                       SlideType.CLOSING, SlideType.TABLE):
                    futs[ex.submit(slotfill_deterministic, item, tree, cat_slide,
                                   toc_entries, doc_title)] = item
                else:
                    futs[ex.submit(slotfill_page, item, tree, cat_slide, client, pm)] = item
            done = len(plan.pages) - len(todo)
            for fut in as_completed(futs):
                fill = fut.result()
                _save_json(pages_dir / f"page_{fill.page_no:02d}.fill.json", fill)
                done += 1
                _check_cancel(job)  # 逐页间隙取消点（已完成页保留可续跑）
                job.set_status(JobStatus.GENERATING, f"fill {done}/{len(plan.pages)}")
                job.save_state()

    fills = [_load_json(pages_dir / f"page_{p.page_no:02d}.fill.json", PageFill)
             for p in sorted(plan.pages, key=lambda x: x.page_no)]
    deck = DeckBuild(meta=BuildMeta(title=doc_title, template=catalog.stem), pages=fills)
    issues = validate_deck_build(deck, catalog)
    if issues:
        raise RuntimeError("DeckBuild 未通过校验：\n" + "\n".join(
            f"p{i.page_no} {i.rule.value}: {i.detail}" for i in issues))
    _save_json(art / "deckbuild.json", deck)

    # ---- render（真模板构建：复制映射页 + XML 槽位替换/删除 + 孤儿清理）----
    _check_cancel(job)
    job.set_status(JobStatus.RENDERED, "render")
    job.save_state()
    pptx = art / "output.pptx"
    build_deck(template_pptx_path(catalog.stem), catalog, deck, pptx)

    # ---- QA：容量已在 slotfill 逐槽门控；建后做页数/遗留/空页核对 + COM PNG ----
    _check_cancel(job)
    job.set_status(JobStatus.QA, "export")
    job.save_state()
    from app.services.com_export import template_checklist

    report: list[str] = [f"[checklist] p{q.page_no} {q.code.value}: {q.detail}"
                         for q in template_checklist(deck, catalog, pptx)]
    if com_export:
        from app.services.com_export import export_pngs

        export_pngs(pptx, job.dir / "pages")
    (art / "qa_report.txt").write_text("\n".join(report) or "（无问题）", encoding="utf-8")

    job.set_status(JobStatus.DONE, f"{len(fills)} 页")
    job.save_state()


def _plan_content(tree: DocTree, docmap: DocMap, client: LLMClient, pm: PromptManager,
                  catalog: TemplateCatalog) -> SlidePlan:
    from app.pipeline.plan import plan_slides

    return plan_slides(tree, docmap, client, pm, catalog)


def _catalog_of(job: _JobLike) -> TemplateCatalog:
    """PPT 线必带 template（stem）；缺失/未知给清晰错误（旧 skin 任务不可续跑）。"""
    stem = getattr(job, "template", None)
    if not stem:
        raise RuntimeError(
            "PPT 任务缺少 template（模板 stem）——真模板渲染链必须指定模板，"
            "请用模板参数重建任务")
    return load_catalog(stem)


def _check_cancel(job: _JobLike) -> None:
    """协作式取消检查点（A2）：job 带 check_cancel 则调用（CLI 桩可省）。"""
    fn = getattr(job, "check_cancel", None)
    if fn is not None:
        fn()


def run_pipeline_safe(job: _JobLike, client: LLMClient | None = None,
                      com_export: bool = True) -> None:
    from app.services.jobs import JobCancelled

    try:
        if getattr(job, "product", "ppt") == "doc":
            from app.pipeline.doc_runner import run_doc_pipeline

            run_doc_pipeline(job, client, com_export)
        else:
            run_pipeline(job, client, com_export)
    except JobCancelled:
        # 协作式取消：cancel() 已把状态落盘 CANCELLED，这里只保证线程干净
        # 退出。已完成阶段产物保留——重跑同源文件零成本复用。
        log.info("job %s 已取消（%s）", job.dir.name,
                 getattr(job, "status", "?").value)
        job.set_status(JobStatus.CANCELLED, "用户取消")
        job.save_state()
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
