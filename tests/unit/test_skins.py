from __future__ import annotations

from app.render.skins import list_skins, load_skin, skin_choices

# (前景, 背景, 最低对比度, 说明) —— WCAG AA：正文/表头 4.5，大字标题 3.0，弱化文字 2.5
_PAIRS = [
    ("text", "bg", 4.5, "正文"),
    ("title", "bg", 3.0, "标题"),
    ("cover_title", "cover_bg", 3.0, "封面标题"),
    ("cover_subtitle", "cover_bg", 3.0, "封面副题"),
    ("header_text", "header_bg", 4.5, "表头"),
    ("text", "zebra", 4.5, "斑马行正文"),
    ("text", "card_bg", 4.5, "卡片正文"),
    ("title", "section_bg", 3.0, "章节页标题"),
    ("accent", "bg", 3.0, "强调色"),
    ("column_heading", "bg", 3.0, "分栏标题"),
    ("muted", "bg", 2.5, "弱化文字"),
    ("footer_text", "bg", 2.5, "页脚"),
]


def _lum(hex6: str) -> float:
    def f(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex6[i:i + 2], 16) / 255 for i in (0, 2, 4))
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)


def _ratio(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_all_shipped_skins_load():
    skins = list_skins()
    assert len(skins) >= 6, "应至少有 6 套皮肤（2 原有 + 4 新增）"
    for name in skins:
        sk = load_skin(name)
        assert sk.name == name


def test_skin_choices_cover_all():
    choices = dict(skin_choices())
    assert set(choices) == set(list_skins())
    assert all(v.strip() for v in choices.values()), "显示名不能为空"


def test_shipped_skins_contrast():
    for name in list_skins():
        sk = load_skin(name)
        for fg, bg, need, desc in _PAIRS:
            got = _ratio(getattr(sk, fg), getattr(sk, bg))
            assert got >= need, f"{name} {desc} {fg}/{bg} 对比度 {got:.2f} < {need}"
