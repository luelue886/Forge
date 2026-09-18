"""10-gram 防抄袭：生成文本不得与源文出现连续 10 字雷同（9 字以内不算）。

两侧都先把数字折叠成占位符 N 再比对——数字是设计上的 verbatim（照抄铁律，
正确性由 numbers.check_numbers 溯源轴另行把关）。"8,642.30万元"这类长数字
自身就 ≥10 字，若计入走向势轴，任何忠实照抄都必然命中，两条铁律自相矛盾。
"""

from __future__ import annotations

import re

from app.qa.numbers import _NUM_TOKEN

_STRIP_WS = re.compile(r"\s+")


def _clean(s: str) -> str:
    return _NUM_TOKEN.sub("N", _STRIP_WS.sub("", s))


def ngram_hits(generated: str, source: str, n: int = 10) -> list[str]:
    """返回命中的源文 n-gram 片段（同一段连续抄袭只报首个窗口，重复片段去重）。"""
    src, gen = _clean(source), _clean(generated)
    if len(src) < n or len(gen) < n:
        return []
    grams = {src[i:i + n] for i in range(len(src) - n + 1)}
    hits: list[str] = []
    seen: set[str] = set()
    in_run = False
    for i in range(len(gen) - n + 1):
        g = gen[i:i + n]
        if g in grams:
            if not in_run and g not in seen:
                seen.add(g)
                hits.append(g)
            in_run = True
        else:
            in_run = False
    return hits
