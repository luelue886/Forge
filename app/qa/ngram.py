"""10-gram 防抄袭：生成文本不得与源文出现连续 10 字雷同（9 字以内不算）。

两侧都先把数字折叠成占位符 N 再比对——数字是设计上的 verbatim（照抄铁律，
正确性由 numbers.check_numbers 溯源轴另行把关）。"8,642.30万元"这类长数字
自身就 ≥10 字，若计入走向势轴，任何忠实照抄都必然命中，两条铁律自相矛盾。

B3 窄豁免：exempt 集合中的串（表头/标题/源文内重复 ≥2 次的模板话术）整串
原样保留不构成抄袭——命中 gram 整体是某豁免串的子串才放行。gram 是完整
10 字窗，无法借豁免串向前后延伸抄袭。
"""

from __future__ import annotations

import re

from app.qa.numbers import _NUM_TOKEN

_STRIP_WS = re.compile(r"\s+")


def exempt_from_tree(tree) -> set[str]:
    """源树的客观事实豁免串集合（B3）：结构上必然原样的单元。

    两个来源：
    1. 各表 header/caption 全串 + 章节标题——表格骨架/标题按设计
       verbatim，改写反而错；
    2. 源文内重复 ≥2 次的格文本（清洗后 ≥10 字）——同一评价框架在源文
       多表重复出现 = 模板固定话术而非独特表达，照搬不构成抄袭（实测
       W-CELL-FALLBACK 主力正是这类）。单次出现的普通格文本绝不豁免
       （那是仿写对象）。
    """
    from app.schema.textlen import text_weight

    out: set[str] = set()
    counts: dict[str, int] = {}

    def _add_counts(s: str) -> None:
        c = _clean(s)
        if len(c) >= 10 and text_weight(c) >= 10:
            counts[c] = counts.get(c, 0) + 1

    for sec in tree.sections:
        for s in sec.walk():
            if s.title:
                out.add(s.title)
    for t in tree.tables:
        if t.caption:
            out.add(t.caption)
        for h in t.header or []:
            if h.strip():
                out.add(h)
        for row in t.rows:
            for cell in row:
                if cell.strip():
                    _add_counts(cell)
    out.update(c for c, n in counts.items() if n >= 2)
    return out


def _clean(s: str) -> str:
    return _NUM_TOKEN.sub("N", _STRIP_WS.sub("", s))


def ngram_hits(generated: str, source: str, n: int = 10,
               exempt: set[str] | None = None) -> list[str]:
    """返回命中的源文 n-gram 片段（同一段连续抄袭只报首个窗口，重复片段去重）。

    exempt：客观事实类固定术语（表头/标题/源内模板话术）。命中 gram 整体
    是某豁免串的子串则放行——n 字窗是某串的子串 ⟺ 该串的某个 n 字窗；
    gram 无法借豁免串向前后延伸抄袭（延伸窗口含豁免串外字符即命中）。
    """
    src, gen = _clean(source), _clean(generated)
    if len(src) < n or len(gen) < n:
        return []
    grams = {src[i:i + n] for i in range(len(src) - n + 1)}
    allowed: set[str] = set()
    if exempt:
        for ex in exempt:
            c = _clean(ex)
            if len(c) >= n:
                allowed.update(c[i:i + n] for i in range(len(c) - n + 1))
    hits: list[str] = []
    seen: set[str] = set()
    in_run = False
    for i in range(len(gen) - n + 1):
        g = gen[i:i + n]
        if g in grams and g not in allowed:
            if not in_run and g not in seen:
                seen.add(g)
                hits.append(g)
            in_run = True
        else:
            in_run = False
    return hits
