import re

_CJK = re.compile("[　-〿一-鿿＀-￯]")


def text_weight(s: str) -> float:
    """汉字当量长度：CJK 字符计 1，其余（字母/数字/半角标点）计 0.5。

    所有"≤N 汉字"类硬规则统一用此函数计量。
    """
    if not s:
        return 0.0
    return sum(1.0 if _CJK.match(ch) else 0.5 for ch in s)
