"""文档线流水线：parse → docplan → ★PLANNED → 逐节 fill → DocIR → QA 修复
→ docx 渲染 → Word COM PDF → PyMuPDF 预览 → DONE。每阶段先查产物，幂等可续跑。"""

from __future__ import annotations

import logging
from pathlib import Path

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.pipeline.docfill import assemble_docir, fill_all_sections
from app.pipeline.docplan import build_doc_plan
from app.pipeline.genre import extract_letter_frame
from app.pipeline.runner import _JobLike, _artifacts, _load_json, _save_json
from app.pipeline.tblvisual import CONTENT_WIDTH_CM
from app.pipeline.tablefill import fill_all_tables
from app.qa.docqa import qa_and_repair
from app.qa.spotcheck import run_spot_check_loop
from app.render.docx_render import render_docir_to_docx
from app.render.pdf_preview import export_pdf_page_pngs
from app.schema.docir import DocIR, TableBlock, validate_docir
from app.schema.docplan import DocPlan
from app.schema.doctree import DocTree
from app.schema.enums import Genre, IssueCode, JobStatus

log = logging.getLogger(__name__)


def _render_source(job: _JobLike) -> Path | None:
    """渲染期源 docx（表格/图片 XML 搬运的锚点）：.docx 原样，.doc 用转换产物。"""
    src = job.dir / "upload" / job.source_name
    if src.suffix.lower() == ".doc":
        src = src.with_name(f"{Path(job.source_name).stem}.converted.docx")
    return src if src.exists() else None


def _patch_doc_tables(doc: DocIR, opinions: list[str], client: LLMClient,
                      pm: PromptManager) -> bool:
    """PDF 源表格布局补丁：意见 → VisualOut（int 校验）→ 应用到 TableBlock。

    返回是否实际改动（False = 无表/调用失败/已收敛）。col_widths 在 DocIR
    存分数（docx_render 按分数用），VisualOut 是百分比 int——进出换算。
    row_heights 约定含表头行（docx_render n_rows 同口径）。
    """
    import json

    from app.schema.tblskeleton import (
        VisualOut,
        renormalize_widths,
        validate_visual,
    )

    def _n_cols(t: TableBlock) -> int:
        return len(t.header) or (len(t.rows[0]) if t.rows else 0)

    def _n_rows(t: TableBlock) -> int:
        return len(t.rows) + (1 if t.header else 0)

    blocks = [b for b in doc.blocks if isinstance(b, TableBlock)]
    if not blocks:
        return False
    summaries = []
    for ti, t in enumerate(blocks):
        cw = [round(w * 100) for w in t.col_widths] if t.col_widths else []
        line = f"表{ti}：{_n_cols(t)} 列 × {_n_rows(t)} 行"
        summaries.append(line + (f"；现列宽% {cw}" if cw else "（现列宽未设）"))
    old_params = json.dumps(
        [{"table_index": ti,
          "col_widths": ([round(w * 100) for w in t.col_widths]
                         if t.col_widths else []),
          "row_heights": t.row_heights or []} for ti, t in enumerate(blocks)],
        ensure_ascii=False)
    try:
        out = client.structured(
            VisualOut,
            [{"role": "system", "content": pm.render("tblvisual/system.md")},
             {"role": "user", "content": pm.render(
                 "spotcheck/patch.j2", feedback=opinions, tables=summaries,
                 old_params=old_params,
                 content_width_cm=CONTENT_WIDTH_CM)}],
            stage="spotcheck-patch")
    except Exception as e:  # noqa: BLE001 — 补丁失败保留原样
        log.warning("表格布局补丁调用失败，保留原参数：%s", e)
        return False
    by_index = {v.table_index: v for v in out.tables}
    changed = False
    for ti, t in enumerate(blocks):
        v = by_index.get(ti)
        if v is None:
            continue
        errors = validate_visual(v, _n_cols(t), _n_rows(t))
        if errors:
            log.warning("表 %d 补丁未过校验，跳过：%s", ti, errors[:2])
            continue
        new_w = [w / 100 for w in renormalize_widths(v.col_widths)]
        if t.col_widths and len(t.col_widths) == len(new_w) and all(
                abs(a - b) < 0.01 for a, b in zip(t.col_widths, new_w)) \
                and (v.row_heights or []) == (t.row_heights or []):
            continue  # 收敛：新参数 ≈ 旧
        t.col_widths = new_w
        if v.row_heights:
            t.row_heights = v.row_heights
        changed = True
    return changed


def _render_doc_outputs(job: _JobLike, doc: DocIR, art: Path,
                        source: Path | None, com_export: bool) -> int:
    """渲染 docx → COM 转 PDF → 逐页 PNG。返回页数（无 COM 记 1）。

    抽检循环的重渲染入口：DocIR 已含补丁后的表格参数。
    """
    job.set_status(JobStatus.RENDERED, "render docx")
    job.save_state()
    docx = render_docir_to_docx(doc, art / "output.docx", source=source)

    n_pages = 1
    if com_export:
        from app.services.com_export import export_docx_pdf

        job.set_status(JobStatus.QA, "导出 PDF")
        job.save_state()
        pdf = export_docx_pdf(docx, art / "output.pdf")
        import pymupdf

        with pymupdf.open(str(pdf)) as d:
            n_pages = len(d)
        pages_dir = job.dir / "pages"
        if pages_dir.exists():
            import shutil

            shutil.rmtree(pages_dir)  # 轮次间页数可能变少，防旧页污染抽样
        export_pdf_page_pngs(pdf, pages_dir)
    return n_pages


def run_doc_pipeline(job: _JobLike, client: LLMClient | None = None,
                     com_export: bool = True) -> None:
    client = client or LLMClient(log_path=job.dir / "logs" / "llm_calls.jsonl")
    pm = PromptManager()
    art = _artifacts(job)

    # ---- docplan（确定性规划 + 体裁识别，文档线唯一 pre-fill LLM 点）----
    docplan_path = art / "docplan.json"
    if not docplan_path.exists():
        tree = _load_json(art / "doctree.json", DocTree)
        job.set_status(JobStatus.UNDERSTOOD, "体裁识别 + 规划")
        job.save_state()
        _save_json(docplan_path, build_doc_plan(tree, client, pm))
        job.save_state()

    plan = _load_json(docplan_path, DocPlan)
    if not (job.confirmed or getattr(job, "auto_confirm", False)):
        job.set_status(JobStatus.PLANNED,
                       f"{len(plan.items)} 节 · 体裁 {plan.genre.value} 待确认")
        job.save_state()
        return

    # ---- form 表格 LLM 重建分支（不限源：docx/.doc/PDF 均走；架构师两轮败
    #      → FormBranchFallback 回落通用链路）----
    if plan.genre is Genre.FORM:
        tree = _load_json(art / "doctree.json", DocTree)
        if tree.tables:
            from app.pipeline.form_branch import (
                FormBranchFallback,
                run_form_branch,
            )

            try:
                run_form_branch(job, client, pm, plan, tree,
                                com_export=com_export)
                return
            except FormBranchFallback as e:
                log.warning("form 分支回落既有链路：%s", e)

    # ---- fill（逐节落盘，可断点续跑）----
    tree = _load_json(art / "doctree.json", DocTree)
    total = len(plan.items)
    job.set_status(JobStatus.GENERATING, f"fill 0/{total}")
    job.save_state()

    def _progress(done: int, _total: int) -> None:
        job.set_status(JobStatus.GENERATING, f"fill {done}/{_total}")
        job.save_state()

    sections = fill_all_sections(plan, tree, client, job.dir / "sections", pm,
                                 on_progress=_progress)

    # ---- QA：数字溯源 + 10-gram，blocking 定向重 fill ≤2 轮 ----
    job.set_status(JobStatus.QA, "数字溯源 + 防抄袭检查")
    job.save_state()
    sections, qa_issues = qa_and_repair(plan, tree, sections, client, pm,
                                        job.dir / "sections")

    # ---- 表格内容仿写（长文本格 ⟦N⟧ 掩码，退格照搬兜底，断点落 artifacts/tables）----
    table_rewrites, tf_report = fill_all_tables(
        tree, client, pm, job.dir / "artifacts" / "tables")

    # ---- 组装 DocIR + 校验 ----
    frame = extract_letter_frame(tree) if plan.genre is Genre.LETTER else None
    doc = assemble_docir(plan, sections, tree, frame, table_rewrites=table_rewrites)
    blocking = [i for i in validate_docir(doc)
                if i.rule is not IssueCode.W_DOC_PARA_LONG]
    if blocking:
        raise RuntimeError("DocIR 未通过校验：\n" + "\n".join(
            f"[{i.rule.value}] {i.detail}" for i in blocking))
    _save_json(art / "docir.json", doc)

    lines = [f"[docqa] {i.section_id} {i.code.value}: {i.detail}"
             for i in qa_issues]
    lines.extend(tf_report)

    # ---- 渲染 docx → Word COM 转 PDF → 逐页预览 ----
    source = _render_source(job)
    if source is None:
        log.warning("渲染期源文件缺失，表格/图片按规范样式重建（排版保真降级）")
    n_pages = _render_doc_outputs(job, doc, art, source, com_export)

    # ---- 视觉抽检 ≤3 轮（PDF 源 + 含表格才有重生成旋钮；docx 源只报告）----
    has_tables = any(isinstance(b, TableBlock) for b in doc.blocks)
    can_patch = tree.meta.source_format == "pdf" and has_tables

    def _patch(opinions: list[str]) -> bool:
        if not can_patch:
            return False
        changed = _patch_doc_tables(doc, opinions, client, pm)
        if changed:
            _save_json(art / "docir.json", doc)  # 补丁后的 DocIR 落盘
        return changed

    def _rerender() -> None:
        nonlocal n_pages
        n_pages = _render_doc_outputs(job, doc, art, source, com_export)

    spot_passed, spot_rounds, spot_rep = run_spot_check_loop(
        job, plan, client, pm, patch=_patch, rerender=_rerender)
    lines += spot_rep
    (art / "qa_report.txt").write_text(
        "\n".join(lines) or "（无问题）", encoding="utf-8")

    detail = f"{len(plan.items)} 节 · {n_pages} 页"
    if spot_rounds:
        detail += (" · 抽检通过" if spot_passed
                   else f" · 抽检 {spot_rounds} 轮未全过（见 qa_report.txt）")
    if qa_issues:
        detail += f" · QA 残留 {len(qa_issues)} 项（见 qa_report.txt）"
    job.set_status(JobStatus.DONE, detail)
    job.save_state()
