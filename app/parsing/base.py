from __future__ import annotations

import re


class ParseError(Exception):
    """解析失败：格式损坏、不受支持、扫描件等。调用方应转为 FAILED + 用户可读信息。"""


def clean_text(s: str) -> str:
    """去零宽字符/BOM 与首尾空白。内部空白保持 verbatim（数字溯源依赖原文形态）。"""
    for ch in ("​", "﻿", "­"):
        s = s.replace(ch, "")
    return s.strip()


_PT_PER_CM = 28.3465

_VML_STYLE = re.compile(
    r"(?:^|;)\s*(width|height)\s*:\s*(-?[\d.]+)\s*(pt|cm|mm|px)\s*(?=;|$)",
    re.IGNORECASE)


def vml_style_size_cm(style: str) -> tuple[float | None, float | None]:
    """v:shape 的 style 属性 → (宽 cm, 高 cm)；任一缺失/不可解析返回 (None, None)。"""
    w = h = None
    for m in _VML_STYLE.finditer(style or ""):
        val, unit = float(m.group(2)), m.group(3).lower()
        cm = (val / _PT_PER_CM if unit == "pt" else val if unit == "cm"
              else val / 10 if unit == "mm" else val / 37.795)  # px 按 96dpi
        if m.group(1).lower() == "width":
            w = cm
        else:
            h = cm
    return w, h


def image_size_class(w_cm: float | None, h_cm: float | None) -> str | None:
    """按物理尺寸分类图片：'icon'（图标/线条）、'portrait'（证件照）、None（正常复用）。

    证件照启发式：近方形（短边/长边 ≥ 0.6）且短边 1.5-4.5cm——常规内容图
    （横版截图/流程图）长宽比或尺寸不落此区间；尺寸未知不猜，保持复用。
    """
    if w_cm is None or h_cm is None:
        return None
    if w_cm <= 0 or h_cm <= 0 or w_cm < 1.0 or h_cm < 1.0:
        return "icon"
    lo, hi = min(w_cm, h_cm), max(w_cm, h_cm)
    if 1.5 <= lo <= 4.5 and lo / hi >= 0.6:
        return "portrait"
    return None
