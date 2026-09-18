from enum import Enum


class SlideType(str, Enum):
    COVER = "cover"
    TOC = "toc"
    SECTION_HEADER = "section_header"
    TEXT_POINTS = "text_points"
    TWO_COLUMN = "two_column"
    TABLE = "table"
    KEY_METRICS = "key_metrics"
    CLOSING = "closing"


class TableStrategy(str, Enum):
    COPY = "copy"
    TRIM = "trim"
    METRICS = "metrics"
    SPLIT = "split"
    DEMOTE = "demote"


class JobStatus(str, Enum):
    PARSED = "PARSED"
    UNDERSTOOD = "UNDERSTOOD"
    PLANNED = "PLANNED"
    GENERATING = "GENERATING"
    RENDERED = "RENDERED"
    QA = "QA"
    REPAIRING = "REPAIRING"
    DONE = "DONE"
    FAILED = "FAILED"


class Severity(str, Enum):
    BLOCKING = "blocking"
    COSMETIC = "cosmetic"


class IssueCode(str, Enum):
    # Schema 硬规则（V-*）
    V_TITLE_LEN = "V-TITLE-LEN"
    V_BULLET_COUNT = "V-BULLET-COUNT"
    V_BULLET_LEN = "V-BULLET-LEN"
    V_METRICS = "V-METRICS"
    V_TABLE_SIZE = "V-TABLE-SIZE"
    V_WHITELIST = "V-WHITELIST"
    V_PAGE_BUDGET = "V-PAGE-BUDGET"
    V_PAGE_NO = "V-PAGE-NO"
    # QA（E-* blocking / W-* cosmetic）
    E_OVERFLOW = "E-OVERFLOW"
    W_CAPACITY_90 = "W-CAPACITY-90"
    E_NUM_UNTRACED = "E-NUM-UNTRACED"
    E_PLAGIARISM = "E-PLAGIARISM"
    E_CROSSPAGE_DUP = "E-CROSSPAGE-DUP"
    E_COVERAGE_MISS = "E-COVERAGE-MISS"
    E_TABLE_OVERSIZE = "E-TABLE-OVERSIZE"
    E_EMPTY_PLACEHOLDER = "E-EMPTY-PLACEHOLDER"
    E_RENDER_MISMATCH = "E-RENDER-MISMATCH"
