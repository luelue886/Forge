from __future__ import annotations

from pathlib import Path

from app.parsing.base import ParseError
from app.parsing.docx_parser import parse_docx
from app.schema.doctree import DocTree

_SUPPORTED = {
    ".docx": "docx",
    ".doc": "doc",
    ".pptx": "pptx",
    ".ppt": "ppt",
    ".pdf": "pdf",
}


def parse_source(path: Path) -> DocTree:
    path = Path(path)
    fmt = _SUPPORTED.get(path.suffix.lower())
    if fmt is None:
        raise ParseError(f"不支持的格式 {path.suffix or '（无后缀）'}，仅支持 docx/doc/pptx/ppt/pdf")
    if not path.exists():
        raise ParseError(f"文件不存在：{path}")
    if fmt in ("docx", "doc"):
        if fmt == "doc":  # 97-2003 二进制：先经 Word COM 转 .docx，落在源文件旁（渲染要搬源 XML）
            converted = path.parent / f"{path.stem}.converted.docx"
            if not converted.exists():
                from app.services.com_export import convert_doc_to_docx

                try:
                    convert_doc_to_docx(path, converted)
                except Exception as e:
                    raise ParseError(
                        f"旧版 .doc 转换失败（需本机装有 Word）：{e}") from e
            tree = parse_docx(converted)
            tree.meta.source_name = path.name  # 展示原名，别露 converted.docx
            tree.meta.parse_warnings.append("旧版 .doc 已经 Word 自动转换为 .docx 后解析")
            return tree
        return parse_docx(path)
    if fmt in ("pptx", "ppt"):
        from app.parsing.pptx_parser import parse_pptx

        if fmt == "ppt":  # 97-2003 二进制：先经 PowerPoint COM 转换（临时目录，不落杂物）
            import tempfile

            from app.services.com_export import convert_to_pptx

            with tempfile.TemporaryDirectory() as td:
                try:
                    converted = convert_to_pptx(path, Path(td) / "converted.pptx")
                except Exception as e:
                    raise ParseError(
                        f"旧版 .ppt 转换失败（需本机装有 PowerPoint）：{e}") from e
                tree = parse_pptx(converted)
            tree.meta.parse_warnings.append("旧版 .ppt 已经 PowerPoint 自动转换为 .pptx 后解析")
            return tree
        return parse_pptx(path)
    if fmt == "pdf":
        from app.parsing.pdf_parser import parse_pdf

        return parse_pdf(path)
    raise ParseError(f"格式 {fmt} 尚未接入")
