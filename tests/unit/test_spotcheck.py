"""视觉抽检：循环控制 / 收敛判停 / 降级 / 产物幂等 / 掩码边界。"""

from __future__ import annotations

import base64
import io
import json
import tempfile
from pathlib import Path

from PIL import Image

from app.llm.prompts import PromptManager
from app.qa.spotcheck import (
    _img_b64,
    actionable,
    check_round,
    run_spot_check_loop,
    sample_pages,
)
from app.schema.docplan import DocPlan
from app.schema.enums import Genre
from app.schema.spotcheck import SpotCheckOut, SpotIssue


# ---- 夹具 ----

class _FakeJob:
    def __init__(self, root: Path):
        self.dir = Path(root)
        self.statuses: list[tuple] = []

    def set_status(self, status, detail: str = "") -> None:
        self.statuses.append((status, detail))

    def save_state(self) -> None:
        pass


def _plan() -> DocPlan:
    return DocPlan(genre=Genre.FORM, title="测试表格文书")


def _issue(area="table", severity="major", problem="第3列过窄文字竖排",
           suggestion="加宽第3列约10%") -> SpotIssue:
    return SpotIssue(area=area, severity=severity, problem=problem,
                     suggestion=suggestion)


def _pages(root: Path, n: int) -> list[Path]:
    d = root / "pages"
    d.mkdir(parents=True, exist_ok=True)
    for i in range(1, n + 1):
        Image.new("RGB", (60, 80), "white").save(d / f"page_{i:02d}.png")
    return sorted(d.glob("page_*.png"))


class _SeqClient:
    """按序弹出 SpotCheckOut；记录消息（供多图/单图断言）。"""

    vision_model = "fake-vision"

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def structured(self, schema, messages, stage=None, **kw):
        self.calls.append({"stage": stage, "messages": messages, "kw": kw})
        if not self._replies:
            raise AssertionError("多余的 LLM 调用")
        return self._replies.pop(0)


class _Boom:
    vision_model = "fake-vision"

    def structured(self, *a, **kw):
        raise RuntimeError("vision down")


# ---- schema ----

def test_schema_parse_and_reject():
    out = SpotCheckOut.model_validate(
        {"passed": False, "issues": [{"area": "table", "severity": "major",
                                      "problem": "x", "suggestion": "y"}]})
    assert out.issues[0].area == "table"
    try:
        SpotCheckOut.model_validate(
            {"passed": True, "issues": [{"area": "chart", "severity": "major",
                                         "problem": "x"}]})
        raise AssertionError("非法 area 应被拒")
    except Exception:
        pass


# ---- 采样与图片 ----

def test_sample_pages_small_all_and_large_even():
    with tempfile.TemporaryDirectory() as td:
        _pages(Path(td), 3)
        assert len(sample_pages(Path(td) / "pages")) == 3
    with tempfile.TemporaryDirectory() as td:
        _pages(Path(td), 10)
        got = sample_pages(Path(td) / "pages")
        assert len(got) == 6
        assert got[0].name == "page_01.png" and got[-1].name == "page_10.png"
        assert [g.name for g in got] == sorted(g.name for g in got)


def test_img_b64_downsamples_wide_png():
    with tempfile.TemporaryDirectory() as td:
        png = Path(td) / "wide.png"
        Image.new("RGB", (2200, 1000), "white").save(png)
        b64 = _img_b64(png)
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        assert img.width <= 1100 and img.format == "JPEG"


# ---- 分类 ----

def test_actionable_table_area_any_severity():
    issues = [_issue(),                       # major+table → 行动
              _issue(severity="minor"),       # minor+table → 行动（列宽微调）
              _issue(area="title"),           # major 非表格 → 只记录
              _issue(area="layout", severity="minor")]
    assert [i.problem for i in actionable(issues)] == [
        issues[0].problem, issues[1].problem]


# ---- 循环控制 ----

def _run(tmp_path, client, patch=None, rerender=None):
    _pages(tmp_path, 2)
    job = _FakeJob(tmp_path)
    patch_calls: list[list[str]] = []
    rerender_calls = [0]

    def _patch(opinions):
        patch_calls.append(opinions)
        return patch() if patch else True

    def _rerender():
        rerender_calls[0] += 1

    got = run_spot_check_loop(job, _plan(), client, PromptManager(),
                              patch=_patch, rerender=_rerender)
    return got, job, patch_calls, rerender_calls[0]


def test_loop_pass_first_round(tmp_path):
    client = _SeqClient([SpotCheckOut(passed=True)])
    (passed, rounds, rep), job, patches, rers = _run(tmp_path, client)
    assert passed and rounds == 1 and rep == []
    assert patches == [] and rers == 0
    art = json.loads((tmp_path / "artifacts" / "spotcheck.json")
                     .read_text(encoding="utf-8"))
    assert art["mode"] == "vision" and len(art["rounds"]) == 1
    assert job.statuses[-1][1] == "排版抽检 R1/3"


def test_loop_fail_patch_then_pass(tmp_path):
    client = _SeqClient([SpotCheckOut(passed=False, issues=[_issue()]),
                         SpotCheckOut(passed=True)])
    (passed, rounds, rep), _, patches, rers = _run(tmp_path, client)
    assert passed and rounds == 2 and rers == 1
    assert len(patches) == 1 and "加宽第3列" in patches[0][0]
    assert any("R1 FAIL" in line for line in rep)
    assert any("[major][table]" in line for line in rep)


def test_loop_three_fails_deliver_last(tmp_path):
    bad = SpotCheckOut(passed=False, issues=[_issue()])
    client = _SeqClient([bad, bad, bad])
    (passed, rounds, rep), _, patches, rers = _run(tmp_path, client)
    assert not passed and rounds == 3 and rers == 2  # R3 后不再重渲染
    assert any("R3 FAIL" in line for line in rep)


def test_loop_converged_early_stop(tmp_path):
    bad = SpotCheckOut(passed=False, issues=[_issue()])
    client = _SeqClient([bad])
    (passed, rounds, rep), _, patches, rers = _run(
        tmp_path, client, patch=lambda: False)
    assert not passed and rounds == 1 and rers == 0
    assert any("收敛" in line for line in rep)


def test_loop_minor_table_still_cycles(tmp_path):
    """FAIL 轮的 minor[table] 意见也触发重生成（列宽微调是旋钮强项）。"""
    client = _SeqClient([
        SpotCheckOut(passed=False, issues=[_issue(severity="minor")]),
        SpotCheckOut(passed=True)])
    (passed, rounds, rep), _, patches, rers = _run(tmp_path, client)
    assert passed and rounds == 2 and rers == 1 and len(patches) == 1


def test_loop_minor_non_table_no_cycle(tmp_path):
    """FAIL 轮只有非表格意见（标题/版式无旋钮）→ 记录不循环。"""
    bad = SpotCheckOut(passed=False, issues=[_issue(area="layout",
                                                    severity="minor")])
    client = _SeqClient([bad])
    (passed, rounds, rep), _, patches, rers = _run(tmp_path, client)
    assert not passed and rounds == 1 and patches == [] and rers == 0
    assert any("[minor][layout]" in line for line in rep)


def test_loop_degraded_mode_no_block(tmp_path):
    _pages(tmp_path, 2)
    job = _FakeJob(tmp_path)
    passed, rounds, rep = run_spot_check_loop(
        job, _plan(), _Boom(), PromptManager(),
        patch=lambda _: True, rerender=lambda: None)
    assert not passed and rounds == 0
    assert any("W-DOWNGRADE" in line for line in rep)
    art = json.loads((tmp_path / "artifacts" / "spotcheck.json")
                     .read_text(encoding="utf-8"))
    assert art["mode"] == "degraded"


def test_loop_no_pages_no_calls(tmp_path):
    job = _FakeJob(tmp_path)
    passed, rounds, rep = run_spot_check_loop(
        job, _plan(), _Boom(), PromptManager(),
        patch=lambda _: True, rerender=lambda: None)
    assert passed and rounds == 0 and rep == []
    assert not (tmp_path / "artifacts" / "spotcheck.json").exists()


def test_loop_artifact_resume_skip(tmp_path):
    client = _SeqClient([SpotCheckOut(passed=True)])
    _run(tmp_path, client)
    again = _SeqClient([])  # 无回复：若被调用会 AssertionError
    job = _FakeJob(tmp_path)
    passed, rounds, _ = run_spot_check_loop(
        job, _plan(), again, PromptManager(),
        patch=lambda _: True, rerender=lambda: None)
    assert passed and rounds == 1 and again.calls == []


# ---- check_round：多图失败降单图 ----

def test_check_round_multi_fail_falls_back_per_page(tmp_path):
    pages = _pages(tmp_path, 2)

    class _MultiBoom:
        vision_model = "fake-vision"

        def structured(self, schema, messages, stage=None, **kw):
            n_img = sum(1 for c in messages[1]["content"]
                        if c.get("type") == "image_url")
            if n_img > 1:
                raise RuntimeError("multi-image unsupported")
            return SpotCheckOut(passed=True)

    out = check_round(_plan(), pages, _MultiBoom(), PromptManager())
    assert out.passed


def test_messages_contain_images_and_genre():
    """抽检消息携带图片与体裁上下文；掩码边界：消息只含渲染成品。"""
    with tempfile.TemporaryDirectory() as td:
        pages = _pages(Path(td), 1)
        client = _SeqClient([SpotCheckOut(passed=True)])
        check_round(_plan(), pages, client, PromptManager())
        msgs = client.calls[0]["messages"]
        content = msgs[1]["content"]
        assert sum(1 for c in content if c["type"] == "image_url") == 1
        assert "表格文书" in content[0]["text"] and "测试表格文书" in content[0]["text"]
