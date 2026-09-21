# Forge

根据源文档（Word / PDF / PPT）仿写生成新文档或 PPT：**客观事实（数字、日期、专名）与源文档
verbatim 一致，其余措辞改写**。两条产品线共用同一任务框架：文档线（docx/PDF 源 → Word+PDF
产物）与 PPT 线（→ pptx 产物，代码化模板皮肤）。

## 设计立场：Pipeline，不是自由 Agent

LLM 只出现在少数提案点（体裁识别 / 规划 / 填充 / 表格重建 / 修复），其余全部是确定性代码。
LLM 的每个输出都要过确定性校验器，不过则定向重试，再不过退回原文。三条铁律贯穿全系统：

1. **数字永不以明文进 prompt**——所有数字先折叠为 ⟦N⟧ 占位符（全局掩码池），LLM 只见到
   占位符；输出中的占位符由代码回填原值，LLM 抄错也无从抄起。
2. **每个改写单元过 10-gram + 数字溯源复检**——与源文档 10-gram 重合即抄袭，改写后数字集合
   与源不一致即漂移。
3. **校验不过的单元退格照搬原文**——宁可不改，不可改错。退格是终点不是异常。

## Agent Loop

系统里唯一允许"循环"的形态是**带确定性裁判的有界重试**：

```
LLM 提案 → 确定性校验 → 通过？
                        ├─ 是 → 采纳
                        └─ 否 → 错误清单回传 → 定向重试（≤3 轮）→ 仍败 → 退格照搬 / 回落旧链路
```

- LLM **没有工具调用权限，不决定流程**。下一步走哪、失败后怎么办，全部由编排器
  （`doc_runner` / `form_branch` / `runner`）的确定性代码决定。
- 重试不是重发：错误清单 + 上轮产物锚定"最小修改"（表格架构师三轮锚定重试——从头重写
  会修好旧缺失又丢新文本，实测不收敛）。
- 循环有硬上界（单元 1 次、架构师 3 轮、修复 2 轮），出界即降级，绝不无限拉扯。

## 工具层

LLM 触达不到的确定性能力，全部是编排器的"手"：

| 层 | 模块 | 能力 |
|---|---|---|
| LLM 适配 | `app/llm/client.py` | `chat()` / `structured(pydantic schema)` 结构化输出，stage 标签，全量调用落 `llm_calls.jsonl` 审计 |
| 解析 | `app/parsing/` | docx（python-docx + XML）、PDF（pdfplumber，表格 bbox 聚类重建合并区/列宽）、pptx；扫描件拒收 |
| 渲染 | `app/render/` | python-docx（体裁版式）、表格 XML deepcopy 搬运（docx 源排版保真）、form HTML（@page 公文版式）、python-pptx + 皮肤系统（`templates/skins/*.yaml`） |
| COM 服务 | `app/services/com_export.py` | Word/PowerPoint 自动化：.doc→docx、HTML→docx、docx→PDF、pptx→PNG；STA 专用队列 + 超时击杀自恢复 |
| QA | `app/qa/` | ngram 抄袭检测、数字溯源、逐单元校验器 |
| 提示词 | `prompts/` | Jinja2 模板（system.md + user.j2 + retry.j2），版本随代码 |

## 确定性 Workflow

显式状态机 + 体裁路由，每步幂等：

```
PARSED → UNDERSTOOD → PLANNED（人工确认大纲，可改体裁）→ GENERATING
       → RENDERED → QA →（REPAIRING）→ DONE / FAILED
```

文档线在 PLANNED 之后按 **体裁 × 源格式** 路由：

| 路由 | 走法 |
|---|---|
| docx 源（任意体裁） | 表格 XML 原样搬运保排版，散文掩码仿写 |
| PDF 源 + form 体裁 | **表格分支**：表格架构师（碎片→JSON 骨架）→ 确定性回填 → 内容专家（值格虚构/长格改写）→ 视觉总监（列宽%+行高）→ JSON→HTML → Word COM → docx；架构师失败自动 `FormBranchFallback` 回落通用链路 |
| PDF 源 + letter/report | 通用链路：tablefill 掩码表格仿写 + docfill 散文仿写 |

确认页改体裁是用户的**逃生门**：单文件效果不佳时可强制换链路。PPT 线独立走
docmap → plan → 确认 → 逐页 fill → 渲染 → COM 截图。

## Memory / State

一切状态落盘、可复验、可续跑，无跨任务隐藏状态：

```
data/jobs/<job_id>/
  upload/        源文件
  state.json     状态机（status/detail/error）
  artifacts/     每阶段产物：docplan.json · doctree.json · tblarch.json ·
                 tblcontent.json · tblvisual.json · output.docx/.html/.pdf · qa_report.txt
  pages/         PDF→PNG 逐页预览
  logs/          llm_calls.jsonl（全量调用审计，响应截断 20KB，可离线复检）
```

**产物门控续跑**：每阶段进门先复检已有产物（schema + 覆盖 + 结构校验），有效则跳过 LLM
直接复用，失效则整体重做。进程崩溃后重跑同一任务，已完成阶段零 LLM 成本。每任务一个独立
目录，任务间零共享——排查问题 = 看一个目录，不需要考古。

## QA Loop

三级防线，事前约束、事中拦截、事后兜底：

1. **prompt 约束**（事前）：⟦N⟧ 原样保留禁止新数字、label/header 原样、不编造——写进每个
   system.md。
2. **单元级校验**（事中）：每个改写单元出 LLM 即检——占位符集合与输入一致（防丢防增）→
   回填 → 10-gram 零重合 + 数字溯源一致；值格另加"新值 ≠ 原值"。失败定向重试（只送失败项
   + 原因），再失败退格照搬并落 W 报告行。
3. **终检**（事后）：`qa_report.txt` 汇总 E-（阻断，退格项已兜底）与 W-（保留回声/池外
   文本），e2e 断言零 E-。

门禁：**295 单测 + 6 COM 集成 + 8 golden 端到端样例**（含真实复杂表格 PDF：竖排侧栏重组、
跨页并表、值格虚构+电话照搬）。

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
copy .env.example .env   # 填 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

.venv/Scripts/python.exe -m app llmping            # LLM 连通性 smoke
.venv/Scripts/python.exe -m pytest -m "not com" -q # 单测（COM 测试需本机 Office）
.venv/Scripts/python.exe -m app serve --port 8765  # Web UI：上传/确认/预览/下载
```

CLI 全流程（等价于 Web）：

```bash
.venv/Scripts/python.exe -m app run 源文档.pdf --product doc --genre form --auto-confirm
.venv/Scripts/python.exe -m app status <job_id>
.venv/Scripts/python.exe -m app confirm <job_id> --genre report   # PLANNED 确认 + 改体裁
```

COM 导出（PDF/PNG/HTML→docx）需要本机安装 Word/PowerPoint，运行期间请勿手动打开对应程序。
