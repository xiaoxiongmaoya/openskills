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

examples_zh:
- "分析这些微信聊天记录，整理法律关系和证据"
- "根据聊天记录生成民间借贷案件的起诉状和整套立案材料"
- "申请强制执行，生成强制执行申请书和账户确认书"
examples_en:
- "Analyze these chat records and map the legal relationships"
- "Draft a civil complaint and the full filing document pack"
- "Prepare an enforcement application pack"
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
5. **模板来源**：技能自带 `templates/`（44 条登记：33 份 docx + 11 份 .doc 原件，平铺存放（商店层级限制））。`manage_templates.py --list` 查全表；`.doc` 旧格式从 `_raw_doc/` 复制原件 + 出「填写内容」docx，不改二进制。
6. 聊天截图 OCR 为可选能力：有 OCR 库则识别；没有则标 `needs_review`，由律师人工核对，不把 OCR 猜测当事实。

## 硬边界（不可违反）

1. **模板不得修改**。`templates/` 与 `<工作空间根>/通用件/模板/` 都是只读模板（副本 chmod 444）；
   任何文书都是**复制到 `04未签稿/` 的副本**再填充。禁止原地改模板、禁止在模板里写当事人信息。
   副本被改过用 `manage_templates.py --verify` 会发现，`--restore` 用源覆盖。
2. **开头不问“普通还是要素式”**。顺序恒为：材料 → 分析 → 报告+律师确认清单 → 律师确认 → 此时才问诉状形式 → 生成 Word。
3. **不生成客户确认表 Word**；确认只留痕在 `05案件信息/analysis_confirm.json` 和报告里。
4. **不生成财产线索表**，除非用户明确要求。
5. 执行案件（`case_type=zx`）跳过普通/要素式询问。
6. Skill 目录、工作空间根、桌面/个人目录不写运行产物；产物只落案件目录，模板副本只落 `<工作空间根>/通用件/模板/`。
7. 姓名、证件号、金额不从图片 OCR 或 PDF 取；只从 Word 原稿、明确文本或律师调整项取。
8. 全程不调用 `ensure_login`、不上传、不提交。

## 建议式决策（律师确认清单的默认行为）

报告第 13 节不是让律师从零填写，而是**每项都带建议，律师只选或改**。四项固定决策由
`analyze_case.py` 的 `build_decision_suggestions()` 自动生成，写入 `analysis.json` 的
`decision_suggestions`，并同时反映在第 1 节基本案情与第 13 节。Agent 可在
`agent_findings.json` 写 `decision_suggestions` 覆盖默认值（推荐写法见下）。

1. **案由（建议选择）**：给「推荐 + 备选 + 理由 + 依据」，不写“请律师确认案由”。
   写入 `decision_suggestions.cause = {recommend, alternatives[], reason, basis}`。
   Agent 应结合类案检索给出推荐与备选（如劳务合同纠纷 / 追索劳动报酬纠纷 / 劳动争议）。
2. **管辖法院（建议选择）**：给「推荐法院 + 候选 + 依据」，不写“需律师指定法院”。
   写入 `decision_suggestions.court = {recommend, candidates[], basis}`；依据按民诉法一般地域
   管辖（被告住所地）+ 合同纠纷履行地，具体法院名称与 fyid 由律师在立案平台核对。
3. **律师费（有凭证即主张）**：脚本扫 `evidence_package.json`，`证明目的`含
   “律师费/代理费/维权费用”且非存疑即自动列入诉讼请求，不再作为待确认项。金额需去重；
   能否获支持属胜诉风险，不影响是否主张。可在 `decision_suggestions.lawyer_fee.claim_text` 覆盖措辞。
4. **逾期利息/资金占用损失（每案必问 + 建议）**：固定每案询问，但基于案情给建议。
   写入 `decision_suggestions.interest = {recommend, start_point, basis}`；
   `start_point` 结合欠条约定的履行期届满日，无约定则参照同省类案以起诉状副本送达日起算，按 LPR。
   这些建议全部来自检索与证据，脚本不臆造金额/法条；无依据字段写“待律师判断”。

`questions` 字段仍可写自定义待确认项，但已被上述四项覆盖的话题（案由/劳动定性/法院/管辖/
律师费/利息/逾期/仲裁前置）会自动从第 13 节去重，不重复列出。

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

# 3.1 ★必做★ 逐张看截图并标注用途（身份证/银行卡/送达地址/还款记录等）
# OCR 不可用时 parse 会把图片标“需人工核对”，绝不能就此跳过——必须 Agent 逐张 ViewImage，
# 把用途写进 05案件信息/image_labels.json
#   键=03图片件/xxx.jpg，值={"用途","类型","证据价值","建议文件名"}。
#   「建议文件名」可选：给该图起语义名（不含扩展名/前缀），如 "01-判决书首页"、
#   "03-被告身份证-人像面"，供 3.1b 生成人能一眼看懂的副本名。
python scripts/label_images.py --case-dir "<案件目录>"      # 未标注则退出码 1

# 3.1b 可选：按「建议文件名」生成语义名副本（原件保留，引用不断链）
python scripts/rename_images.py --case-dir "<案件目录>" --dry-run   # 先看计划
python scripts/rename_images.py --case-dir "<案件目录>"             # 生成「语义_XXX.jpg」副本
# 副本前缀「语义_」，label_images/parse 自动跳过；原件仍是 chat_evidence/image_labels 的 key。
# 清理副本：rename_images.py --case-dir "<案件目录>" --clean

# 3.2 整理证据包 + 证据目录（按证明目的分组，无关/存疑单列）
python scripts/build_evidence_package.py --case-dir "<案件目录>"

# 3.3 先落初步争点（检索要用）：06运行记录/agent_findings.json 里至少写
#     {"case_summary": {"cause": "劳务合同纠纷"}, "dispute_focus": [{"focus": "是否存在事实劳务合同关系"}]}
#     争点写成“争什么”的短句，不要写当事人姓名 —— 它会直接进第三方检索词

# 4 双源法律/案例检索：一键生成调用参数（按案由+争点自动出词，当事人姓名脱敏）
#   执行案件（case_type=zx）只检索法条、不检索类案：执行依据为生效判决，
#   类案检索命中的多是「追加被执行人」等无关裁判，无参考价值且多耗 2 次 MCP。
#   plan/ingest-all 会自动读 spec.case_type 决定只跑 law 还是 law+case。
#   前置：Agent 已经读完材料，先把 case_summary.cause 与 dispute_focus 落成
#   06运行记录/agent_findings.json（只写这两项也行），检索词从它生成；不写则回退到 --cause/--focus
python scripts/yuandian_search.py probe --case-dir "<案件目录>" \
  --tools-json "<06运行记录/resolved_tools.json>" --providers yuandian,qcc
python scripts/auto_search.py plan --case-dir "<案件目录>" [--province "浙江省"] [--focus "可覆盖争点"]
# → 05案件信息/search_plan.json（查询词+依据）、05案件信息/mcp_calls/{provider}_{kind}.json
#   Agent 按 calls 逐条 DeferExecuteTool，把原始返回原样落 06运行记录/mcp_raw/{provider}_{kind}.json
#   法条以元典召回为主，企查查法条仅作交叉印证（易泛化召回铁路/涉外劳务，报告按法域取舍）
python scripts/auto_search.py ingest-all --case-dir "<案件目录>" --refresh
# → 自动识别 provider/kind、双源合并与冲突标记，并重跑分析报告（缺源会列出 missing 并退出码 1）

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
# 模板只来自技能自带实战包 templates/，复制到 <工作空间根>/通用件/模板/；缺失即报错
python scripts/manage_templates.py --root "<工作空间根>" --case-type sp --cause "民间借贷纠纷"
python scripts/render_documents.py --case-dir "<案件目录>" --case-type sp
# 证据目录默认不生成。需要时：--with-evidence，或 --only evidence 单独出
# python scripts/render_documents.py --case-dir "<案件目录>" --case-type sp --with-evidence

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

## 当事人信息结构（`05案件信息/party_info.json`）

支持单人与多当事人，`scripts/party_info.py` 统一读成标准结构：

```jsonc
// 单人（兼容旧写法）
{"原告":"张三","身份证号":"...","电话":"...","送达地址":"...",
 "收款户名":"...","收款开户行":"...","收款账号":"...","_待核对":["..."]}

// 多当事人（推荐，个案有多个原告/被告时用）
{"当事人":[
   {"角色":"原告","姓名":"张三","身份证号":"...","电话":"...","送达地址":"...",
    "收款户名":"...","收款开户行":"...","收款账号":"..."},
   {"角色":"被告","姓名":"李四"}
 ],
 "_来源":{...},"_待核对":[...]}
```

模板里的“原告/被告”姓名栏在多当事人时用顿号连接；账户、送达等只填主当事人（第一个），需要每人各出一份确认书时单独生成。

## 分析框架按案件领域切换

`case_summary.case_domain` 取 `civil`（默认）/ `criminal` / `administrative`，或由案由关键词自动判断（含“罪”→刑事，含“行政”→行政）。报告第 4 章框架与第 6 章争议焦点表头随之切换：

| 领域 | 第4章框架 | 争议焦点表头 |
|---|---|---|
| 民事 | 法律关系五步骤 | 原告观点 / 被告观点 |
| 刑事 | 犯罪构成四要件 + 阻却事由 | 控方观点 / 辩方观点 |
| 行政 | 行政行为合法性五要素 | 原告观点 / 行政机关观点 |

## 输出清单

- **一审 sp**：起诉状（按案由取模板，模板标题自带案由，不填 `{{案由}}`）、送达地址确认书、**当事人银行账户确认书**（bank_account 表格版，不用诉讼费退费版）、授权委托书（有代理时）、提交材料清单.md。
- **执行 zx**：强制执行申请书（申请事项走强制执行措辞：请求履行生效文书+支付执行款+加倍迟延利息）、送达地址确认书、**当事人银行账户确认书**（同 sp，用 bank_account docx 表格版，旧 payee_account .doc 已弃用）、授权委托书（有代理时）、提交材料清单.md。
- **证据目录**：默认不生成，两种案件都要显式 `--with-evidence` 或 `--only evidence` 才出。
- **可选**（明确要求才做）：财产保全、延期/线上开庭、撤诉、追加被告、执行结案、网络查控、情况说明、仲裁三件套、债权转移、劳务委托书。统一出口：`render_documents.py --case-dir <案> --case-type sp --extra preservation,postpone_hearing`，`--list-extra` 列全部 key（当前 14 个）。正文取 `06运行记录/extra_docs.json`；**申请事项**有固定程式的由 `EXTRA_DEFAULT_APPLY` 给标准请求语；**事实与理由一律不给默认理由**——理由必须回答「为什么提这个申请」，模型/律师没写就留空并在清单列「需补写事由」，绝不拿诉状案情复述顶替。
- **诉状模板选择**：默认通用多当事人版 `complaint_general`；只有律师明确要要素式且案由有对应模板才用 `complaint_elements_*`，否则一律 `complaint_general`。
- **核心填充规则**：诉请与事实一律由脚本生成、措辞固定（`complaint_text.json` 不得覆盖诉请/案由/法院，只可微调事实与理由）；无值留空并清理空字段标点，禁止写「待补/待确认」；多当事人各占一行、多余行删除；性别/出生日期可从身份证号推导；证据清单优先由证据包生成正式目录，模型可在 `complaint_text.json` 写 `证据清单` 列表直接填表。
- **模板缺失只跳过不崩**：记入 `manifest.skipped` 并在清单顶部提示，其余照常生成。
- **改渲染逻辑或新增模板后必跑自测**：`python scripts/selftest.py --case-dir <案件目录>`（33 份模板实填压测 + 四份主文书复核，退出码非 0 即失败）。踩过的坑见 @references/known-issues.md。
- 填充细节（跨 run 占位符、被告防串号、字段别名、追加段落取舍、落款日期、证据清单分组、模板净化 `template_hygiene.py` 等）：@references/document-rendering.md。
- `.doc` 旧格式模板无法程序化写入：复制原件，另出 `<同名>-填写内容.docx`，由律师手工填入。一页式空白表格（线上开庭、先行调解、债权转移、劳务委托）同样正文需手工填写；`案号` 立案后才有，固定留空。
- 所有 Word 生成到 `04未签稿/`，保留签名/日期/捺印/盖章占位。
- 机器状态在 `05案件信息/`：`chat_evidence.json`、`analysis.json`、`analysis_confirm.json`、`document_manifest.json`、`mcp_status.json`。

### 脚本与模型的分工（重要）

不要把一切都写进脚本。判断标准是「这件事需不需要理解个案」：

| 交给脚本（确定性、重复性） | 交给模型（需理解、需表达） |
|---|---|
| 当事人身份信息拼装、防串号 | 事实与理由的叙述与详略 |
| 诉讼请求编号、固定措辞、债务性质取值 | 证据条目的名称与证明内容措辞 |
| 日期/落款/换行/标点等格式统一 | 各类申请书的事由（延期、撤诉、保全原因） |
| 无值留空、不写「待补」 | 哪些材料该提交、证明对象怎么归纳 |
| 多当事人拆行、重复凭证合并 | 案情定性、争点取舍、风险提示 |

- 模型写内容的入口：`06运行记录/complaint_text.json`（诉状与证据清单）、`06运行记录/extra_docs.json`（按需文书正文）。**身份信息、诉请编号、案由、受理法院不接受覆盖**，其余字段写了就以模型为准。
- 模板没预留占位符、脚本又填不了的结构（自由排版段落、特殊表格），**允许模型直接编辑生成的 docx**（`workbuddy-local-office-edit` 的 editor_sdk / python-docx），不必为它专门写脚本；改完仍要过一遍「无 `{{}}` 残留 + 敏感扫描」。
- 脚本给的默认文案只是兜底，永远不该成为「模型没写就只能这样」的天花板。
- **身份证 OCR 置信度规则**：身份证截图可 OCR 取字段，但**高置信度才写入当事人信息，低置信度留空并提示手填**。判定以可验证性为准，不靠主观（`scripts/ocr_id_card.py`）：身份证号 18 位且校验位正确=high 可落库；校验错=low 留空；姓名 2-4 汉字且与登记一致=high；性别/出生日期与身份证号推导值一致=high 不一致=low；民族/住址=medium（可填标待核）。Agent 视觉读图后把结果写入 `05案件信息/id_ocr_input.json`，再 `ocr_id_card.py --apply` 落库，低置信度字段自动进 `_待核对` 提示手填。

## 合规红线

1. **生成文书前必须逐张看完截图并标注用途**。OCR 不可用不等于可以跳过——身份证号、银行卡号、送达地址常只在截图里。`render_documents.py` 会跑 `label_images` 体检，有未标注图片直接拒绝生成。某劳务合同纠纷案就是漏看截图，导致身份证/银行卡/地址没填进去，必须避免重犯。身份证字段按上文「OCR 置信度规则」处理：高置信度落库、低置信度留空提示手填。
2. 事实必须有来源定位；低置信度 OCR 标 `needs_review`，不作为确定事实。
3. 报告里的图片必须写清用途（`image_labels.json`），不能只写“需人工核对”了事。
4. 法条/案号必须来自 MCP 检索并带 `source_id`；检索未连接或未命中不写结论。
5. 不虚构、不内置本地知识库、不以记忆冒充检索结果，不照搬其他 Skill 的作者/律所信息。
6. **模板只认技能自带实战包**（`templates/`，44 条登记：33 份 docx + 11 份 .doc 原件，平铺存放（商店层级限制），已脱敏）。禁止联网下载、禁止生成“骨架/自造模板”顶替；模板缺失就报错并让用户补文件。
7. **真实当事人信息不得进入技能目录、文档、测试与提交**。姓名、身份证、手机、银行卡、
   完整案号、到门牌住址、聊天原文，一律只留在 `<工作空间根>/案件库/`；写文档/示例/测试样例时
   换成张三、李四、110101199001010000、13800000000、123456789012345678、（2025）浙0000民初1号。
   提交前跑 `python ../scripts/check_sensitive.py --staged`（仓库 pre-commit 已强制拦截）；
   检索词外发前当事人姓名替换为“当事人”（`auto_search.py` 已内置）。
8. 输出附：本报告为基于所提供材料的辅助分析，不构成正式法律意见；事实以证据原件为准，法条与类案以检索来源为准，最终结论以律师确认为准。

## 参考文档

- @references/workspace-layout.md — 目录规范与产物落点
- @references/analysis-method.md — 两阶段五步骤与报告模板
- @references/mcp-yuandian.md — 元典 MCP 工具、参数、未连接暂停
- @references/mcp-qcc.md — 企查查法律数据工具、参数、双源交叉
- @references/templates-registry.md — 实战模板包来源、脱敏规则、登记表
- @references/document-slots.md — 文书与法院槽位映射
- @references/document-rendering.md — 文书渲染细节（改模板/改填充逻辑时查阅）
- @references/known-issues.md — 渲染踩过的坑与已落地防御

## 许可

源码开放；个人研究、学习、非商业用途免费使用；**任何商业用途需单独获得作者授权**。协议全文见仓库根目录 [LICENSE](../LICENSE)（PolyForm Noncommercial License 1.0.0）。
