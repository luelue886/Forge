"""form+PDF 表格 LLM 重建分支编排（表格架构师 → 内容专家 → 视觉总监
→ JSON→HTML → Word COM 存 docx → sanity 不过 → python-docx 兜底渲染
→ PDF/PNG → DONE）。

三阶段产物（tblarch/tblcontent/tblvisual.json）幂等门控：存在且复检
通过即跳过 LLM。架构师两次重试仍败 → FormBranchFallback 由 doc_runner
捕获，整文档回落既有链路。骨架本体永不改动：内容专家的改写在渲染前
套用到 deepcopy，断点产物始终对 pristine 骨架可复检。
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.pipeline.runner import _JobLike
from app.pipeline.tblarch import FormBranchFallback, run_architect
from app.pipeline.tblcontent import MIN_CELL_WEIGHT, run_content
from app.pipeline.tblvisual import CONTENT_WIDTH_CM, run_visual
from app.render.docx_render import render_docir_to_docx
from app.render.form_html import FormBlock, build_form_html
from app.render.pdf_preview import export_pdf_page_pngs
from app.schema.docir import (
    DocIR,
    DocIRMeta,
    DocTitleBlock,
    ImageBlock,
    ParaBlock,
    TableBlock,
)
from app.schema.docplan import DocPlan
from app.schema.doctree import DocTree
from app.schema.enums import JobStatus
from app.schema.tblskeleton import TSkeleton, TVisualTable, walk_grid
from app.schema.textlen import text_weight

log = logging.getLogger(__name__)


def _apply_overlays(skeletons: list[TSkeleton],
                    table_rw: dict[int, dict[str, str]]) -> list[TSkeleton]:
    """deepcopy 骨架 + 套用内容专家改写（"r,c" = 网格坐标锚点）。"""
    out = [s.model_copy(deep=True) for s in skeletons]
    for ti, cells in (table_rw or {}).items():
        if not 0 <= ti < len(out):
            continue
        anchors, _ = walk_grid(out[ti])
        for key, text in cells.items():
            r_s, _, c_s = key.partition(",")
            if r_s.isdigit() and c_s.isdigit():
                cell = anchors.get((int(r_s), int(c_s)))
                if cell is not None:
                    cell.content = text
    return out


def _doc_order(tree: DocTree, skeletons: list[TSkeleton],
               prose_rw: dict[str, str]
               ) -> tuple[list[tuple], list[str]]:
    """文档序中间表示：("para", text) / ("table", ti) / ("image", image_id)。

    表块经 src_tables 映射骨架：首个碎片位置产出骨架，吸收掉的碎片跳过；
    未被任何骨架吸收的碎片记 W 行（内容已由覆盖校验把关，此处防丢表）。
    未在文档序出现的骨架尾部补挂。散文键 p{i} 与 _prose_candidates 同序。
    """
    frag_owner: dict[str, int] = {}
    for ti, s in enumerate(skeletons):
        for tid in s.src_tables:
            frag_owner.setdefault(tid, ti)
    warnings: list[str] = []
    placed: set[int] = set()
    pi = 0
    items: list[tuple] = []
    for sec in tree.sections:
        for s in sec.walk():
            for b in s.blocks:
                if b.kind in ("para", "list_item"):
                    text = b.text
                    if text_weight(text) >= MIN_CELL_WEIGHT:
                        text = prose_rw.get(f"p{pi}", text)
                        pi += 1
                    if text.strip():
                        items.append(("para", text))
                elif b.kind == "table" and b.table_id:
                    ti = frag_owner.get(b.table_id)
                    if ti is None:
                        warnings.append(
                            f"[formbranch] W-FRAG-UNPLACED {b.table_id}: "
                            f"碎片未被骨架吸收，未渲染")
                    elif ti not in placed:
                        placed.add(ti)
                        items.append(("table", ti))
                elif b.kind == "image" and b.image_id:
                    items.append(("image", b.image_id))
    for ti in range(len(skeletons)):
        if ti not in placed:
            items.append(("table", ti))
    return items, warnings


def _pdf_image_b64(pdf_path: Path, im) -> tuple[str, float] | None:
    """PDF 源图片 → base64 PNG + 相对版心宽的显示宽（cm）。"""
    import base64

    import pymupdf

    try:
        with pymupdf.open(str(pdf_path)) as pdf:
            if im.page is None or not 1 <= im.page <= len(pdf):
                return None
            page = pdf[im.page - 1]
            clip = pymupdf.Rect(im.bbox) if im.bbox else None
            pix = page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(2, 2))
            src_w = clip.width if clip else page.rect.width
            width_cm = CONTENT_WIDTH_CM * src_w / page.rect.width
            return (base64.b64encode(pix.tobytes("png")).decode("ascii"),
                    width_cm)
    except Exception as e:  # noqa: BLE001 — 图片失败降级跳过，不崩整篇
        log.warning("pdf 图片 %s 区域渲染失败：%s", im.image_id, e)
        return None


def _form_blocks(items: list[tuple], tree: DocTree,
                 pdf_src: Path | None) -> list[FormBlock]:
    images = {im.image_id: im for im in tree.images}
    out: list[FormBlock] = []
    for it in items:
        if it[0] == "para":
            out.append(FormBlock(kind="para", text=it[1]))
        elif it[0] == "table":
            out.append(FormBlock(kind="table", table_index=it[1]))
        elif it[0] == "image":
            im = images.get(it[1])
            got = _pdf_image_b64(pdf_src, im) if im and pdf_src else None
            if got is not None:
                out.append(FormBlock(kind="image", b64=got[0],
                                     width_cm=got[1]))
    return out


def _skeleton_table_block(ti: int, s: TSkeleton,
                          v: TVisualTable | None) -> TableBlock:
    """骨架 → TableBlock（展开网格；merges 由 span 推导；行高 atLeast）。

    首行含 header 样式格 → header 行，否则整表为数据行；merges 坐标与
    含表头行的网格坐标一致（docx_render 约定）。
    """
    anchors, _ = walk_grid(s)
    n_rows = len(s.rows)
    texts = [["" for _ in range(s.total_cols)] for _ in range(n_rows)]
    merges: list[list[int]] = []
    for (r, c), cell in sorted(anchors.items()):
        texts[r][c] = cell.content
        if cell.rowspan > 1 or cell.colspan > 1:
            merges.append([r, c, cell.rowspan, cell.colspan])
    has_header = any(cell.style == "header" and r == 0
                     for (r, _), cell in anchors.items())
    return TableBlock(
        table_id=f"skel-{ti}",
        header=texts[0] if has_header else [],
        rows=texts[1:] if has_header else texts,
        col_widths=[w / 100 for w in v.col_widths] if v is not None else None,
        merges=merges or None,
        row_heights=v.row_heights if v is not None else None,
    )


def _fallback_docir(plan: DocPlan, items: list[tuple], tree: DocTree,
                    skeletons: list[TSkeleton],
                    visuals: list[TVisualTable]) -> DocIR:
    """python-docx 兜底渲染用 DocIR：图片走 PDF 区域位图（source=pdf 源）。"""
    images = {im.image_id: im for im in tree.images}
    blocks: list = [DocTitleBlock(text=plan.title)]
    for it in items:
        if it[0] == "para":
            blocks.append(ParaBlock(text=it[1]))
        elif it[0] == "table":
            ti = it[1]
            v = visuals[ti] if ti < len(visuals) else None
            blocks.append(_skeleton_table_block(ti, skeletons[ti], v))
        elif it[0] == "image" and it[1] in images:
            im = images[it[1]]
            blocks.append(ImageBlock(image_id=im.image_id, page=im.page,
                                     bbox=im.bbox))
    return DocIR(meta=DocIRMeta(title=plan.title, genre=plan.genre),
                 blocks=blocks)


def _docx_ok(path: Path, title: str,
             skeletons: list[TSkeleton]) -> list[str]:
    """输出 sanity：可开、有表、合并格落 XML、A4 公文边距、无 ⟦⟧、标题在。"""
    from docx import Document

    try:
        d = Document(str(path))
    except Exception as e:  # noqa: BLE001
        return [f"docx 无法打开：{e}"]
    if not d.tables:
        return ["输出无表格"]
    errors: list[str] = []
    has_span = any(cell.colspan > 1 or cell.rowspan > 1
                   for s in skeletons for row in s.rows for cell in row.cells)
    if has_span:
        xml = d.element.xml
        if "gridSpan" not in xml and "vMerge" not in xml:
            errors.append("骨架含合并格但输出 XML 无 gridSpan/vMerge")

    def _near(a: float, b: float) -> bool:
        return abs(a - b) <= 0.2

    sec = d.sections[0]
    if not _near(sec.page_width.cm, 21.0) or not _near(sec.page_height.cm, 29.7):
        errors.append(
            f"页面非 A4：{sec.page_width.cm:.2f}×{sec.page_height.cm:.2f}cm")
    for name, got, want in (("上", sec.top_margin.cm, 2.54),
                            ("下", sec.bottom_margin.cm, 2.54),
                            ("左", sec.left_margin.cm, 3.18),
                            ("右", sec.right_margin.cm, 3.18)):
        if not _near(got, want):
            errors.append(f"{name}边距 {got:.2f}cm ≠ {want}cm")
    full = "\n".join(p.text for p in d.paragraphs)
    full += "\n" + "\n".join(cell.text for t in d.tables
                             for row in t.rows for cell in row.cells)
    if "⟦" in full:
        errors.append("输出残留 ⟦N⟧ 占位符")
    if title and title not in full:
        errors.append("标题缺失")
    return errors


def run_form_branch(job: _JobLike, client: LLMClient, pm: PromptManager,
                    plan: DocPlan, tree: DocTree,
                    com_export: bool = True) -> None:
    art = job.dir / "artifacts"
    art.mkdir(parents=True, exist_ok=True)

    # ① 表格架构师（产物幂等；两轮败 → FormBranchFallback 由 doc_runner 捕获）
    job.set_status(JobStatus.GENERATING, "表格架构重建")
    job.save_state()
    skeletons, _pool, rep_arch = run_architect(tree, plan, client, pm, art)

    # ② 内容专家（骨架格 + 散文仿写）
    job.set_status(JobStatus.GENERATING, "表格内容仿写")
    job.save_state()
    table_rw, prose_rw, rep_content = run_content(
        skeletons, tree, client, pm, art)

    # ③ 视觉总监（列宽/行高；失败确定性兜底，不阻断）
    visuals, rep_visual = run_visual(skeletons, tree, client, pm, art)

    # ④ 文档序 + 改写套用（骨架本体不动，产物对 pristine 骨架可复检）
    final_skeletons = _apply_overlays(skeletons, table_rw)
    items, frag_warn = _doc_order(tree, skeletons, prose_rw)
    pdf_src = job.dir / "upload" / job.source_name
    pdf_src = pdf_src if pdf_src.exists() else None

    # ⑤ HTML → Word COM → sanity；不过 → python-docx 兜底渲染
    job.set_status(JobStatus.RENDERED, "render html→docx")
    job.save_state()
    docx = art / "output.docx"
    errors: list[str] = []
    if com_export:
        try:
            from app.services.com_export import convert_html_to_docx

            html_path = art / "output.html"
            html_path.write_text(build_form_html(
                plan.title, _form_blocks(items, tree, pdf_src),
                final_skeletons, visuals), encoding="utf-8-sig")
            convert_html_to_docx(html_path, docx)
            errors = _docx_ok(docx, plan.title, skeletons)
        except Exception as e:  # noqa: BLE001 — COM 失败 → python-docx 兜底
            log.warning("HTML→docx 转换失败，走 python-docx 兜底：%s", e)
            errors = [f"COM 转换失败：{e}"]
    else:
        errors = ["无 COM 环境"]
    if errors:
        log.warning("form 分支 sanity 未过（%s），python-docx 兜底渲染",
                    errors[:3])
        render_docir_to_docx(
            _fallback_docir(plan, items, tree, final_skeletons, visuals),
            docx, source=pdf_src)
        recheck = _docx_ok(docx, plan.title, final_skeletons)
        if recheck:
            raise RuntimeError(
                "form 分支兜底渲染仍未过 sanity：" + "；".join(recheck[:5]))

    # ⑥ PDF 导出 + 逐页预览 + QA 报告 → DONE
    n_pages = 1
    if com_export:
        from app.services.com_export import export_docx_pdf

        job.set_status(JobStatus.QA, "导出 PDF")
        job.save_state()
        pdf = export_docx_pdf(docx, art / "output.pdf")
        import pymupdf

        with pymupdf.open(str(pdf)) as d:
            n_pages = len(d)
        export_pdf_page_pngs(pdf, job.dir / "pages")

    lines = rep_arch + rep_content + rep_visual + frag_warn
    (art / "qa_report.txt").write_text(
        "\n".join(lines) or "（无问题）", encoding="utf-8")
    detail = f"{len(skeletons)} 表 · {n_pages} 页"
    if lines:
        detail += f" · QA 残留 {len(lines)} 项（见 qa_report.txt）"
    job.set_status(JobStatus.DONE, detail)
    job.save_state()
