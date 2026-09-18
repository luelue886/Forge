# AIGC 文档仿写 PPT Agent

根据源文档（Word / PDF / PPT）仿写生成 PPT：客观事实（数字、日期、专名）与源文档
verbatim 一致，其余措辞改写；输出一律使用自建模板库。

架构：Pipeline（非自由 Agent）。LLM 只在 docmap / planner / fill / repair 四点出现，
其余全部确定性代码。核心数据流：

源文档 → DocTree → DocMap(LLM) → SlidePlan(LLM) → 大纲确认 → SlideIR(LLM 逐页)
→ 校验 → 渲染(python-pptx) → PowerPoint COM 截图 → QA → 有界修复 → 交付

## 快速开始

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
.venv/Scripts/python.exe -m pytest tests -q

# 渲染一份手写 SlideIR（W1 脊柱，零 LLM）
.venv/Scripts/python.exe -m app render tests/fixtures/deck_all_types.json \
  --skin business_blue --out out.pptx --pngs out_pngs
```

注意：`--pngs` 与 COM 清单检查需要本机安装 PowerPoint；运行期间请勿手动打开 PowerPoint。

## 皮肤（模板）

模板为代码化设计系统：`app/render/design.py` 定义 8 种页面类型的统一几何，
`templates/skins/*.yaml` 定义配色皮肤。新增皮肤 = 新增一个 yaml。

## 状态

- [x] W1 脊柱：Schema/校验器/设计系统/渲染器/容量预估/COM
- [ ] W2 干净输入端到端（docx/pptx 解析 → docmap → plan → fill）
- [ ] W3 表格子系统 + PDF + 大纲确认流
- [ ] W4 QA 全家 + 修复循环 + 单页重生成
