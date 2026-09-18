"""10-gram 防抄袭：生成文本不得与源文出现连续 10 字雷同（9 字以内不算）。"""

from __future__ import annotations

import re

_STRIP_WS = re.compile(r"\s+")


def _clean(s: str) -> str:
    return _STRIP_WS.sub("", s)


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
