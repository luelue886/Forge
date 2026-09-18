"""PDF → 逐页 PNG 预览（PyMuPDF，不经 COM）。"""

from __future__ import annotations

from pathlib import Path


def export_pdf_page_pngs(pdf_path: Path, out_dir: Path,
                         width_px: int = 1000) -> list[Path]:
    """整份 PDF 导出 page_NN.png（按页序），返回路径列表。"""
    import pymupdf

    pdf = pymupdf.open(str(Path(pdf_path).resolve()))
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    try:
        for i, page in enumerate(pdf, 1):
            scale = width_px / page.rect.width
            pix = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale))
            png = out / f"page_{i:02d}.png"
            pix.save(str(png))
            paths.append(png)
    finally:
        pdf.close()
    return paths
