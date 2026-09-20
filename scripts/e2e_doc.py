"""6 golden 样例 e2e：run --product doc --auto-confirm → DONE + 仿写硬断言。

断言（独立复检，不信任流水线自报）：
  1) 状态 DONE，qa_report 无 E- 级残留
  2) heading+para 数字 100% 溯源（check_numbers）
  3) heading+para 与源文 10-gram 零命中（ngram_hits）
  4) 表格：header 逐字一致；改写格（在 artifacts/tables 的 cells 里）ngram 零命中
     + 数字溯源；其余格 == 源 grid 照搬；断点产物齐套
  5) 产物齐套：output.docx / output.pdf / pages/*.png，PNG 数 == PDF 页数
  6) 排版保真（docx 源）：逐表 tblGrid 列宽与逐行 (gridSpan, vMerge) 序列
     与源一致；解析期保留的图片（tree.images）必须出现在输出 media
  7) 样例特定：resume 值格已换 + 证件照已删；form_personnel.pdf 的
     merges 与 docx 版已知结构一致，输出合并指纹与 docx 版一致

用法：.venv/Scripts/python.exe scripts/e2e_doc.py [样例路径 ...]
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = ROOT / ".venv" / "Scripts" / "python.exe"
GOLDEN = [
    ROOT / "examples" / "letter_application.docx",
    ROOT / "examples" / "sectioned_report.docx",
    ROOT / "examples" / "text_report.pdf",
    ROOT / "examples" / "form_personnel.docx",
    ROOT / "examples" / "resume_sample.docx",
    ROOT / "examples" / "form_personnel.pdf",
]


def _tbl_signatures(docx_path: Path):
    """逐表 (gridCol 宽度列表, 逐行 [(gridSpan, vMerge), …])——排版指纹。"""
    from docx import Document
    from docx.oxml.ns import qn

    d = Document(str(docx_path))
    out = []
    for tbl in d.tables:
        grid = tbl._tbl.find(qn("w:tblGrid"))
        widths = [gc.get(qn("w:w")) for gc in grid.findall(qn("w:gridCol"))]
        rows = []
        for tr in tbl._tbl.findall(qn("w:tr")):
            sig = []
            for tc in tr.findall(qn("w:tc")):
                tc_pr = tc.find(qn("w:tcPr"))
                span, vm = 1, None
                if tc_pr is not None:
                    gs = tc_pr.find(qn("w:gridSpan"))
                    if gs is not None and (gs.get(qn("w:val")) or "").isdigit():
                        span = int(gs.get(qn("w:val")))
                    v = tc_pr.find(qn("w:vMerge"))
                    if v is not None:
                        vm = v.get(qn("w:val")) or "continue"
                sig.append((span, vm))
            rows.append(sig)
        out.append((widths, rows))
    return out


def _merge_signature(docx_path: Path):
    """逐表合并指纹：逐行 [(gridSpan, vMerge), …]（不含列宽——PDF 重建路径
    列宽按版心重排，与源 docx 绝对宽度必然不同）。"""
    return [rows for _, rows in _tbl_signatures(docx_path)]


def _zip_media(docx_path: Path) -> list[str]:
    import zipfile

    with zipfile.ZipFile(docx_path) as z:
        return [n for n in z.namelist() if n.startswith("word/media/")]


def run_sample(src: Path) -> tuple[bool, list[str]]:
    from app.config import DATA_DIR
    from app.qa.ngram import ngram_hits
    from app.qa.numbers import check_numbers
    from app.schema.doctree import DocTree

    errors: list[str] = []
    r = subprocess.run(
        [str(PY), "-m", "app", "run", str(src), "--product", "doc", "--auto-confirm"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=900)
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return False, [f"CLI 失败（rc={r.returncode}）：\n{out[-1500:]}"]

    job_id = next((l.split("=", 1)[1].strip() for l in out.splitlines()
                   if l.startswith("job_id=")), None)
    if not job_id:
        return False, [f"未解析到 job_id：\n{out[-500:]}"]
    job_dir = DATA_DIR / "jobs" / job_id
    art = job_dir / "artifacts"

    state = json.loads((job_dir / "state.json").read_text(encoding="utf-8"))
    if state["status"] != "DONE":
        return False, [f"状态 {state['status']}：{state.get('error')}"]

    docir = json.loads((art / "docir.json").read_text(encoding="utf-8"))
    tree = DocTree.model_validate_json(
        (art / "doctree.json").read_text(encoding="utf-8"))

    # 1) 数字 100% 溯源 + 2) 10-gram 零命中（逐块独立复检：
    #    块间拼接会把"段尾句号+下个标题"凑成伪 10-gram，渲染产物里块本是分行）
    for b in docir["blocks"]:
        if b["kind"] not in ("heading", "para") or not b.get("text", "").strip():
            continue
        for tok, ctx in check_numbers(b["text"], tree.full_text):
            errors.append(f"[E-NUM-UNTRACED] {tok}: {ctx}")
        for g in ngram_hits(b["text"], tree.full_text):
            errors.append(f"[E-PLAGIARISM] 与源文连续雷同：{g}")

    # 3) 表格内容分类断言：header 逐字；改写格（断点产物 cells）零雷同 + 数字
    #    溯源；其余格 == 源 grid 照搬
    src_tables = {t.table_id: t for t in tree.tables}
    for b in docir["blocks"]:
        if b["kind"] != "table":
            continue
        tid = b["table_id"]
        t = src_tables.get(tid)
        if t is None:
            errors.append(f"[TABLE-DIFF] {tid} 不在源文档")
            continue
        if b["header"] != list(t.header):
            errors.append(f"[TABLE-DIFF] {tid} header 与源表不一致")
        src_rows = [list(r) for r in t.rows]
        got_rows = b["rows"]
        if len(got_rows) != len(src_rows) or any(
                len(g) != len(s) for g, s in zip(got_rows, src_rows)):
            errors.append(f"[TABLE-DIFF] {tid} 行列结构与源表不一致")
            continue
        art_path = art / "tables" / f"{tid}.json"
        if not art_path.exists():
            errors.append(f"[TABLE-DIFF] {tid} 缺 tablefill 断点产物")
            continue
        rewrites = json.loads(
            art_path.read_text(encoding="utf-8"))["cells"]
        for r, (got_row, src_row) in enumerate(zip(got_rows, src_rows)):
            for c, (got, want) in enumerate(zip(got_row, src_row)):
                key = f"{r},{c}"
                if key in rewrites:
                    if got != rewrites[key]:
                        errors.append(
                            f"[TABLE-DIFF] {tid} {key} 与断点产物不一致")
                    for tok, ctx in check_numbers(got, tree.full_text):
                        errors.append(
                            f"[E-NUM-UNTRACED] {tid} {key} {tok}: {ctx}")
                    for g in ngram_hits(got, tree.full_text):
                        errors.append(
                            f"[E-PLAGIARISM] {tid} {key} 与源文连续雷同：{g}")
                elif got != want:
                    errors.append(
                        f"[TABLE-DIFF] {tid} {key} 非改写格与源不一致"
                        f"：{got!r} != {want!r}")

    # 4) QA 报告无 E- 级残留（W- 级如长段提示/表格退格不算失败）
    qa = (art / "qa_report.txt").read_text(encoding="utf-8")
    residuals = [l for l in qa.splitlines() if "[docqa]" in l and " E-" in l]
    if residuals:
        errors.append(f"QA 残留 {len(residuals)} 项：\n" + "\n".join(residuals[:5]))

    # 5) 产物齐套
    docx, pdf = art / "output.docx", art / "output.pdf"
    if not docx.exists() or docx.stat().st_size < 4000:
        errors.append("output.docx 缺失或过小")
    if not pdf.exists() or pdf.stat().st_size < 4000:
        errors.append("output.pdf 缺失或过小")
    else:
        import pymupdf

        with pymupdf.open(str(pdf)) as d:
            n_pages = len(d)
        pngs = sorted((job_dir / "pages").glob("page_*.png"))
        if len(pngs) != n_pages:
            errors.append(f"预览 PNG {len(pngs)} 张 ≠ PDF {n_pages} 页")

    from docx import Document

    d = Document(str(docx))
    if len(d.paragraphs) < 2:
        errors.append(f"docx 段落数异常（{len(d.paragraphs)}）")

    # 6) 排版保真（docx 源）：逐表 tblGrid 列宽 + 逐行合并结构与源一致；
    #    解析期保留的图片（tree.images，证件照/图标已在解析期过滤）必须
    #    出现在输出 media
    if src.suffix.lower() == ".docx":
        src_docx = job_dir / "upload" / src.name
        if not src_docx.exists():
            errors.append("upload 源 docx 缺失，无法比对排版")
        else:
            if _tbl_signatures(src_docx) != _tbl_signatures(docx):
                errors.append("[LAYOUT-DIFF] 表格 tblGrid/合并结构与源不一致")
    if len(_zip_media(docx)) < len(tree.images):
        errors.append(
            f"[LAYOUT-DIFF] 输出 media {_zip_media(docx)} 少于解析保留的 "
            f"{len(tree.images)} 张图片")

    # 7) 样例特定断言
    if src.name == "resume_sample.docx":
        art_path = art / "tables" / "tbl-001.json"
        cells = (json.loads(art_path.read_text(encoding="utf-8"))["cells"]
                 if art_path.exists() else {})
        if "0,1" not in cells:
            errors.append("[E-VALUE] 姓名值格未被虚构改写（0,1 不在改写产物）")
        elif cells["0,1"] == "张三":
            errors.append("[E-VALUE] 姓名值格照抄原值")
        if _zip_media(docx):
            errors.append("[E-VALUE] 输出仍含图片（证件照未删除）")
    elif src.name == "form_personnel.pdf":
        want = [[2, 0, 1, 2], [2, 2, 1, 2], [3, 0, 2, 1], [5, 0, 1, 4]]
        all_merges = sorted(m for t in tree.tables for m in (t.merges or []))
        if all_merges != want:
            errors.append(f"[LAYOUT-DIFF] PDF 合并区与 docx 版不一致：{all_merges}")
        ref = ROOT / "examples" / "form_personnel.docx"
        if _merge_signature(docx) != _merge_signature(ref):
            errors.append("[LAYOUT-DIFF] 输出合并指纹与 docx golden 不一致")

    return not errors, errors


def main(argv: list[str]) -> int:
    targets = [Path(a) for a in argv] or GOLDEN
    ok_all = True
    for src in targets:
        if not src.exists():
            print(f"[skip] 样例不存在：{src}")
            ok_all = False
            continue
        print(f"\n===== {src.name} =====")
        ok, errs = run_sample(src)
        print("[PASS]" if ok else "[FAIL]")
        for e in errs:
            print(f"  {e}")
        ok_all = ok_all and ok
    print("\n" + ("全部通过" if ok_all else "存在失败"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
