"""视觉抽检：渲染页 PNG → 视觉 LLM 排版判定 → 意见驱动的排版参数重生成（≤3 轮）。

内容永不因抽检重生成（已被确定性 QA 三重把关）；重生成的只有排版参数——
form 分支走视觉总监带意见重跑，通用链路 PDF 源走表格布局补丁。抽检 LLM
读到的是渲染成品（含真实数字），但其输出只流向视觉参数（int 校验）与
报告文本，永不回流文档内容，不构成数字污染。

判定只认 major+table 为可行动意见（minor 记录不循环）；3 轮不过交付
最后一版；视觉调用失败降级报告模式，绝不阻断 DONE。

断点：artifacts/spotcheck.json 存在且已出循环 → 整段跳过。
"""

from __future__ import annotations

import base64
import io
import json
import logging
from collections.abc import Callable
from pathlib import Path

from app.llm.client import LLMClient
from app.llm.prompts import PromptManager
from app.pipeline.runner import _JobLike
from app.schema.docplan import DocPlan
from app.schema.enums import Genre, JobStatus
from app.schema.spotcheck import SpotCheckOut, SpotIssue

log = logging.getLogger(__name__)

MAX_ROUNDS = 3
SAMPLE_MAX = 6
IMG_MAX_W = 1100

_GENRE_LABEL = {Genre.FORM: "表格文书", Genre.LETTER: "书信/申请书",
                Genre.REPORT: "报告/总结"}


def sample_pages(pages_dir: Path) -> list[Path]:
    """≤6 页全查；>6 均匀抽 6（首尾必含）。"""
    pages = sorted(pages_dir.glob("page_*.png"))
    if len(pages) <= SAMPLE_MAX:
        return pages
    idx = sorted({round(i * (len(pages) - 1) / (SAMPLE_MAX - 1))
                  for i in range(SAMPLE_MAX)})
    return [pages[i] for i in idx]


def _img_b64(png: Path) -> str:
    from PIL import Image

    img = Image.open(png)
    if img.width > IMG_MAX_W:
        img = img.resize((IMG_MAX_W, round(img.height * IMG_MAX_W / img.width)))
    img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _messages(pm: PromptManager, plan: DocPlan, n_pages: int,
              imgs: list[str]) -> list[dict]:
    system = pm.render("spotcheck/system.md")
    user = pm.render("spotcheck/user.j2",
                     genre=plan.genre.value,
                     genre_label=_GENRE_LABEL.get(plan.genre, plan.genre.value),
                     title=plan.title, n_pages=n_pages)
    content: list[dict] = [{"type": "text", "text": user}]
    content += [{"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b}"}}
                for b in imgs]
    return [{"role": "system", "content": system},
            {"role": "user", "content": content}]


def check_round(plan: DocPlan, pages: list[Path], client: LLMClient,
                pm: PromptManager) -> SpotCheckOut:
    """一轮检查：优先多图单调用，端点不支持降单图逐页（首页败即抛）。"""
    imgs = [_img_b64(p) for p in pages]
    try:
        return client.structured(
            SpotCheckOut, _messages(pm, plan, len(imgs), imgs),
            stage="spotcheck", max_attempts=2, model=client.vision_model)
    except Exception as e:  # noqa: BLE001 — 多图失败降单图
        log.warning("多图抽检调用失败，降单图逐页：%s", e)
    outs = []
    for i, img in enumerate(imgs):
        outs.append(client.structured(
            SpotCheckOut, _messages(pm, plan, 1, [img]),
            stage=f"spotcheck-p{i + 1}", max_attempts=2,
            model=client.vision_model))
    issues = [x for o in outs for x in o.issues]
    return SpotCheckOut(passed=all(o.passed for o in outs), issues=issues)


def actionable(issues: list[SpotIssue]) -> list[SpotIssue]:
    """表格类意见才有重生成旋钮（major/minor 均可——列宽/行高微调正是
    minor 级问题的修法）；非表格（标题/段落/版式）只记录不循环。"""
    return [i for i in issues if i.area == "table"]


def _load_artifact(path: Path) -> tuple[bool, int, list[str]] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        rounds = data["rounds"]
        report = [str(x) for x in data.get("report", [])]
        passed = bool(rounds) and bool(rounds[-1]["passed"])
        return passed, len(rounds), report
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_artifact(path: Path, mode: str, rounds: list[dict],
                   report: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"schema": "spotcheck/1.0", "mode": mode, "rounds": rounds,
         "report": report}, ensure_ascii=False, indent=2), encoding="utf-8")


def run_spot_check_loop(job: _JobLike, plan: DocPlan, client: LLMClient,
                        pm: PromptManager, *,
                        patch: Callable[[list[str]], bool],
                        rerender: Callable[[], None]
                        ) -> tuple[bool, int, list[str]]:
    """渲染页视觉抽检 ≤3 轮。返回 (最终是否通过, 检查轮数, 报告行)。

    patch(意见文本列表) -> 是否实际改了排版参数（False = 无旋钮/已收敛，
    循环判停）；rerender() 重产 docx/pdf/png（参数已变，仅重渲染）。
    """
    art_path = job.dir / "artifacts" / "spotcheck.json"
    got = _load_artifact(art_path)
    if got is not None:
        return got

    rounds: list[dict] = []
    report: list[str] = []
    passed = False
    mode = "vision"
    for r in range(1, MAX_ROUNDS + 1):
        pages = sample_pages(job.dir / "pages")  # 每轮重抽：重渲染后页数可能变
        if not pages:
            return True, 0, []  # 无渲染页（无 COM 环境）不构成失败
        job.set_status(JobStatus.QA, f"排版抽检 R{r}/{MAX_ROUNDS}")
        job.save_state()
        try:
            verdict = check_round(plan, pages, client, pm)
        except Exception as e:  # noqa: BLE001 — 视觉不可用 → 降级报告模式
            log.warning("视觉抽检不可用，降级报告模式：%s", e)
            mode = "degraded"
            report.append(f"[spotcheck] W-DOWNGRADE 视觉抽检不可用：{e}")
            break
        rounds.append(verdict.model_dump())
        if not verdict.passed:  # 通过不进 qa_report（DONE detail 已示"抽检通过"）
            report.append(f"[spotcheck] R{r} FAIL")
        for i in verdict.issues:
            line = f"[spotcheck] R{r} [{i.severity}][{i.area}] {i.problem}"
            report.append(line + (f"（建议：{i.suggestion}）" if i.suggestion else ""))
        if verdict.passed:
            passed = True
            break
        acts = actionable(verdict.issues)
        if not acts or r == MAX_ROUNDS:
            break
        opinions = [f"{i.problem}（建议：{i.suggestion}）" for i in acts]
        if not patch(opinions):
            report.append(f"[spotcheck] R{r} 排版参数已收敛/无旋钮，提前结束")
            break
        rerender()
    _save_artifact(art_path, mode, rounds, report)
    return passed, len(rounds), report
