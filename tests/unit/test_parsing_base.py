from __future__ import annotations

import pytest

from app.parsing.base import image_size_class, vml_style_size_cm


def test_image_size_class():
    # 图标/线条：任一边 < 1cm 或退化
    assert image_size_class(0.5, 5.0) == "icon"
    assert image_size_class(0.0, 3.0) == "icon"
    # 证件照：近方形（短/长 ≥ 0.6）且短边 1.5-4.5cm
    assert image_size_class(2.8, 3.5) == "portrait"
    assert image_size_class(3.5, 2.8) == "portrait"
    assert image_size_class(3.0, 4.5) == "portrait"
    # 常规内容图：保持复用
    assert image_size_class(14.0, 3.2) is None   # 横幅（golden form_personnel）
    assert image_size_class(6.0, 6.0) is None    # 短边超出证件照上限
    assert image_size_class(1.2, 3.0) is None    # 短边过窄
    assert image_size_class(2.0, 5.0) is None    # 长宽比悬殊
    # 尺寸未知：不猜，保持复用
    assert image_size_class(None, 3.0) is None
    assert image_size_class(None, None) is None


def test_vml_style_size_cm():
    w, h = vml_style_size_cm("width:79.4pt;height:99.2pt")
    assert w == pytest.approx(2.8, abs=0.01) and h == pytest.approx(3.5, abs=0.01)
    assert vml_style_size_cm("position:absolute;width:3cm;height:4cm") == (3.0, 4.0)
    assert vml_style_size_cm("width:30mm;height:1.2cm") == (3.0, 1.2)
    w, h = vml_style_size_cm("width:80px;height:40px")
    assert w == pytest.approx(2.12, abs=0.01) and h == pytest.approx(1.06, abs=0.01)
    assert vml_style_size_cm("") == (None, None)
    assert vml_style_size_cm("width:3cm") == (3.0, None)  # 缺高 → 不猜
