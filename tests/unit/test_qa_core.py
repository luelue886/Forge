from __future__ import annotations

from app.qa.numbers import check_numbers, extract_number_tokens
from app.qa.ngram import ngram_hits


# ---- 数字溯源 ----

def test_traced_with_thousands_separator_and_fullwidth():
    src = "全年营收 1,234.56 万元，签约客户 １２８ 家。"
    gen = "报告期内营业收入为 1234.56 万元，客户数量达 128 家。"
    assert check_numbers(gen, src) == []


def test_fabricated_number_flagged():
    src = "全年营收 1,234.56 万元。"
    gen = "全年营收 1,234.56 万元，利润 987 万元。"
    hits = check_numbers(gen, src)
    assert [t for t, _ in hits] == ["987"]
    assert "利润" in hits[0][1]


def test_small_count_whitelist_and_boundary():
    src = "全年营收 1,234.56 万元。"
    assert check_numbers("覆盖 3 个大区，团队共 99 人。", src) == []  # ≤99 豁免
    assert [t for t, _ in check_numbers("覆盖 100 个大区。", src)] == ["100"]


def test_no_substring_false_positive():
    src = "项目编号 2012。"  # 源只有 2012 这个 token
    assert [t for t, _ in check_numbers("项目编号 20123。", src)] == ["20123"]


def test_date_tokens_each_traceable():
    src = "申请日期：2026 年 9 月 18 日。"
    assert check_numbers("本人于 2026年9月18日 提交申请。", src) == []


def test_percent_and_decimal():
    src = "同比增长 8.5%，净利润率 12%。"
    assert check_numbers("同比增幅 8.5%，净利率 12%。", src) == []
    assert [t for t, _ in check_numbers("同比增幅 8.6%。", src)] == ["8.6"]


def test_extract_tokens_normalization():
    assert extract_number_tokens("１，２３４.５６ 与 78%") == ["1234.56", "78"]


# ---- 10-gram ----

def test_ngram_10_char_copy_hits():
    src = "该季度公司营业收入大幅增长且超出市场预期目标。"
    gen = "概述：该季度公司营业收入大幅增长且超出市场预期目标，情况良好。"
    hits = ngram_hits(gen, src)
    assert len(hits) == 1
    assert hits[0] == "该季度公司营业收入大幅增长且超出"[:10]


def test_ngram_9_char_copy_passes():
    src = "季度营收增长且超出预期。" * 2
    gen = "本季营收增长且超出预期水平。"  # 最长公共连续 9 字
    assert ngram_hits(gen, src) == []


def test_ngram_whitespace_ignored():
    src = "该季度公司营业收入大幅增长且超出市场预期目标。"
    gen = "该季度公司营业收入 大幅增长且超出市场预期目标。"
    assert len(ngram_hits(gen, src)) == 1


def test_ngram_long_copy_reports_once():
    src = "一二三四五六七八九十一二三四五六七八九十"
    gen = "引子：" + src + "结尾。"
    assert ngram_hits(gen, src) == ["一二三四五六七八九十"]
