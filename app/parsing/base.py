from __future__ import annotations


class ParseError(Exception):
    """解析失败：格式损坏、不受支持、扫描件等。调用方应转为 FAILED + 用户可读信息。"""


def clean_text(s: str) -> str:
    """去零宽字符/BOM 与首尾空白。内部空白保持 verbatim（数字溯源依赖原文形态）。"""
    for ch in ("​", "﻿", "­"):
        s = s.replace(ch, "")
    return s.strip()
