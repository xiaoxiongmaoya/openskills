---
name: case-analysis-document-pack

display_name: "案件分析与立案材料包"

display_name_en: "Case Analysis & Filing Document Pack"

description: "把聊天记录、合同、判决书等材料分析成有来源的法律关系报告，经律师确认后再按选择生成整套可打印签字的立案文书；不登录法院、不上传、不提交。当用户要求分析案件、看法律关系、整理聊天记录证据、生成起诉状/执行申请书/送达地址确认书/账户确认书/证据目录/授权委托书/财产保全/延期开庭等情况说明时使用。"

description_zh: "以法律关系分析法两阶段五步骤为核心，结合华宇元典法规/类案检索，输出带来源标注的案件分析报告；律师确认分析结果后，才询问普通/要素式诉状，并生成整套可打印签字的 Word 材料包。全程不登录法院、不上传、不提交。"

description_en: "Analyze case materials with source-linked legal-relationship analysis, optional Yuandian legal/case retrieval, and after lawyer confirmation generate a complete printable/signable Word filing pack. Never logs in, uploads, or submits to court."

category: "writing"

version: "1.1.0"

author: "case-assistant"
---

# 案件分析与立案材料包

把一堆材料（尤其是聊天记录）分析清楚，经律师确认后，输出**整套可打印签字 Word**。本技能只做分析和出文件，**不登录法院、不上传、不提交**。

## 适用 / 不适用

- **适用**：案件分析、法律关系梳理、聊天记录证据化、起诉状/强制执行申请书、送达地址确认书、账户确认书、证据目录、授权委托书、提交材料清单。
- **不适用**：在线立案、材料上传、法院状态查询、最终提交——这些交回 `court-civil-first-instance-filing`。

## 前置条件

1. **至少连接一个第三方法律数据源**：华宇元典（`yuandian-mcp`）+ 企查查法律数据（`qcc-legal`），两者均已实测可用，推荐双源交叉。只连一个源时报告标“单源检索结果”。**本技能不内置本地知识库、不内置案由规则卡**，构成要件、抗辩、证据清单、法答网口径全部来自第三方检索。
2. 真实工具名：元典 `yuandian_semantic_search_legal_articles` / `yuandian_semantic_search_cases`；企查查 `mcp__qcc-legal__get_legal_article_semantic_search` / `mcp__qcc-legal__get_judicial_case_search`。返回结构见 @references/mcp-yuandian.md、@references/mcp-qcc.md。
3. 一个源都没连：**暂停法律/案例检索**，报告只写有来源的事实，法律与类案章节留占位并标“待核验”。**严禁虚构法条、案号、法院**。
4. 工具名会随显示名漂移：先用 `ToolSearch(tool_names=[...])` 反查真实全名，再用 `DeferExecuteTool` 调用。详见 @references/mcp-yuandian.md 与 @references/mcp-qcc.md。
5. **模板来源**：技能自带 `assets/templates/`（44 个律师实战文书，已脱敏）。`manage_templates.py --list` 查全表；`.doc` 旧格式只复制原件 + 出「填写内容」docx，不改二进制。
6. 聊天截图 OCR 为可选能力：有 OCR 库则识别；没有则标 `needs_review`，由律师人工核对，不把 OCR 猜测当事实。

## 硬边界（不可违反）

1. **模板不得修改**。`assets/templates/` 与 `<工作空间根>/通用件/模板/` 都是只读模板（副本 chmod 444）；
   任何文书都是**复制到 `04未签稿/` 的副本**再填充。禁止原地改模板、禁止在模板里写当事人信息。
   副本被改过用 `manage_templates.py --verify` 会发现，`--restore` 用源覆盖。
2. **开头不问“普通还是要素式”**。顺序恒为：材料 → 分析 → 报告+律师确认清单 → 律师确认 → 此时才问诉状形式 → 生成 Word。
3. **不生成客户确认表 Word**；确认只留痕在 `05案件信息/analysis_confirm.json` 和报告里。
4. **不生成财产线索表**，除非用户明确要求。
5. 执行案件（`case_type=zx`）跳过普通/要素式询问。
6. Skill 目录、工作空间根、桌面/个人目录不写运行产物；产物只落案件目录，模板副本只落 `<工作空间根>/通用件/模板/`。
7. 姓名、证件号、金额不从图片 OCR 或 PDF 取；只从 Word 原稿、明确文本或律师调整项取。
8. 全程不调用 `ensure_login`、不上传、不提交。

## 快速开始

```bash
# 0 依赖自检（Python 3.10+；只用标准库 + 已有 Skill 的离线组件）
python --version

# 1 建案骨架（复用老 Skill；法院未定时用 待确认 / 0000）
python ../court-civil-first-instance-filing/scripts/bootstrap_case.py \
  --root "<工作空间根>" --fyid 0000 --court "待确认" --region "" \
  --cause "民间借贷纠纷" --plaintiff "张三" --defendant "李四" --case-type sp

# 2 整理材料：截图→03图片件，导出文本/Word→01原始包/04未签稿（复用老 Skill）
python ../court-civil-first-instance-filing/scripts/organize_materials.py \
  --case-dir "<案件目录>" --source "<客户给的目录或zip>"

# 3 解析聊天记录（截图 + 导出 txt/docx）
python scripts/parse_chat_records.py --case-dir "<案件目录>"

# 4 双源法律/案例检索（Agent 层：ToolSearch 反查 → DeferExecuteTool；脚本只构造与校验）
python scripts/yuandian_search.py probe --case-dir "<案件目录>" \
  --tools-json "<06运行记录/resolved_tools.json>" --providers yuandian,qcc
# 元典：法规语义 + 类案
python scripts/yuandian_search.py build-call --provider yuandian --kind law \
  --query "民间借贷逾期利息 利率保护上限" --out "<06运行记录/call_yuandian_law.json>"
python scripts/yuandian_search.py build-call --provider yuandian --kind case \
  --query "民间借贷 微信聊天记录 借款合意 实际交付" --province "浙江省" \
  --out "<06运行记录/call_yuandian_case.json>"
# 企查查：法条语义 + 普通案例（权威案例另调 get_judicial_authoritative_case_search）
python scripts/yuandian_search.py build-call --provider qcc --kind law \
  --query "民间借贷逾期利息 利率保护上限" --out "<06运行记录/call_qcc_law.json>"
python scripts/yuandian_search.py build-call --provider qcc --kind case \
  --query "民间借贷 微信聊天记录 借款合意 实际交付" --province "浙江省" \
  --out "<06运行记录/call_qcc_case.json>"
# Agent 执行 4 个 MCP 调用后把原始返回落盘，再分别 ingest（自动双源合并/冲突标记）
python scripts/yuandian_search.py ingest --provider yuandian --kind law --raw "<06运行记录/yuandian_law_raw.json>" --case-dir "<案件目录>"
python scripts/yuandian_search.py ingest --provider yuandian --kind case --raw "<06运行记录/yuandian_case_raw.json>" --case-dir "<案件目录>"
python scripts/yuandian_search.py ingest --provider qcc --kind law --raw "<06运行记录/qcc_law_raw.json>" --case-dir "<案件目录>"
python scripts/yuandian_search.py ingest --provider qcc --kind case --raw "<06运行记录/qcc_case_raw.json>" --case-dir "<案件目录>"

# 4.5 定稿前引用核验（企查查回库校验，去幻觉）
# Agent 层把报告/诉状中所有“《法名》第 N 条”整段传入：
#   mcp__qcc-legal__get_legal_citation_verify(text=...)
# 返回中未命中的引用 = 未能核验，必须删改或标存疑；失效/已被修订的提示替换。

# 5 离线管辖候选（不登录法院）
python scripts/suggest_court_offline.py --case-dir "<案件目录>"

# 6 生成分析报告（--findings 为 Agent 结构化分析结论，可选；会自动跑完整性检查）
python scripts/analyze_case.py --case-dir "<案件目录>" --findings "<06运行记录/agent_findings.json>"
python scripts/check_completeness.py --case-dir "<案件目录>" --json

# 7 律师确认分析结果 → 留痕（一审此时先不传 complaint-format）
python scripts/confirm_gate.py --case-dir "<案件目录>" --confirm [--adjustments-json <json>]

# 8 ★确认之后才问★：一审问“普通版 / 要素式”，执行跳过；选定后回写
python scripts/confirm_gate.py --case-dir "<案件目录>" --confirm --complaint-format ordinary
# 或 --complaint-format elements

# 9 准备模板 + 渲染整套 Word（确认门禁不过会直接失败）
# 模板只来自技能自带实战包 assets/templates/，复制到 <工作空间根>/通用件/模板/；缺失即报错
python scripts/manage_templates.py --root "<工作空间根>" --case-type sp --cause "民间借贷纠纷"
python scripts/render_documents.py --case-dir "<案件目录>" --case-type sp

# 10 离线门禁（不联网、不建档）
python ../court-civil-first-instance-filing/scripts/preflight_materials.py \
  --case-dir "<案件目录>" --case-type sp
```

## 复用与规避

| 老 Skill 组件 | 用法 |
| --- | --- |
| `bootstrap_case.py` | 直接调用，建立 `案件库/` 与 `通用件/` |
| `organize_materials.py` | 直接调用，只复制不删除、GBK 转码 |
| `read_office_text.py` | 直接调用，Word 优先提取 |
| `preflight_materials.py` | 直接调用，离线门禁 |
| `write_case_note.py` | 可选调用，刷新 `案件说明.md` |
| `filing_runner.py` 的 `SLOT_ALIAS/EXTRA_COPY` | 由 `doc_slots.py` 只读解析常量，不运行 main |
| `court_client.py` 的 `EXEC_SLOTS` | 由 `doc_slots.py` 只读解析常量，不运行 main |
| `suggest_court.py` | **不调用**（`L148-151` 会 `ensure_login`）；用 `suggest_court_offline.py` |

内部工具：`scripts/docx_helper.py` 用标准库生成/填充 docx，无 `python-docx` 依赖。

## 输出清单

- **一审 sp**：起诉状（普通版 / 要素式二选一，按案由挑模板）、送达地址确认书、账户确认书、证据目录、授权委托书（有代理时）、提交材料清单.md。
- **诉状填充硬规则**：普通版用通用占位符模板（`民事起诉状-通用版.docx`，7 个占位符：原告/原告身份信息/被告/被告身份信息/诉讼请求/事实与理由/受理法院）。**保留模板全部原文与版式，只替换占位符**；**有什么写什么、没有的留空**，禁止写"待补/待确认/（待补充）"，禁止往模板末尾追加"填写内容"重复段落，禁止为个案拆分占位符（如身份信息 A/B、诉请 1/2）。占位符值优先取 `06运行记录/complaint_text.json`，Agent 只写真实内容。
- **执行 zx**：强制执行申请书、送达地址确认书、收款账户确认书、证据目录、授权委托书（有代理时）、提交材料清单.md。
- **可选**（明确要求才做）：财产保全、延期/线上开庭、撤诉、追加被告、执行结案、网络查控、情况说明、仲裁三件套、债权转移、劳务委托书。模板见 `manage_templates.py --list`。
- `.doc` 旧格式模板无法程序化写入：复制原件，另出 `<同名>-填写内容.docx`，由律师手工填入。
- 所有 Word 生成到 `04未签稿/`，保留签名/日期/捺印/盖章占位。
- 机器状态在 `05案件信息/`：`chat_evidence.json`、`analysis.json`、`analysis_confirm.json`、`document_manifest.json`、`mcp_status.json`。

## 合规红线

1. 事实必须有来源定位；低置信度 OCR 标 `needs_review`，不作为确定事实。
2. 法条/案号必须来自 MCP 检索并带 `source_id`；检索未连接或未命中不写结论。
3. 不虚构、不内置本地知识库、不以记忆冒充检索结果，不照搬其他 Skill 的作者/律所信息。
4. **模板只认技能自带实战包**（`assets/templates/`，44 个文书，已脱敏）。禁止联网下载、禁止生成“骨架/自造模板”顶替；模板缺失就报错并让用户补文件。
5. 输出附：本报告为基于所提供材料的辅助分析，不构成正式法律意见；事实以证据原件为准，法条与类案以检索来源为准，最终结论以律师确认为准。

## 参考文档

- @references/workspace-layout.md — 目录规范与产物落点
- @references/analysis-method.md — 两阶段五步骤与报告模板
- @references/mcp-yuandian.md — 元典 MCP 工具、参数、未连接暂停
- @references/mcp-qcc.md — 企查查法律数据工具、参数、双源交叉
- @references/templates-registry.md — 实战模板包来源、脱敏规则、登记表
- @references/document-slots.md — 文书与法院槽位映射
