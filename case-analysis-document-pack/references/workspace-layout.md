# 目录规范与产物落点

本技能沿用 `court-civil-first-instance-filing/references/workspace-layout.md` 的冻结规范，不另起一套。

## 三层边界

| 层 | 路径 | 权限 |
| --- | --- | --- |
| Skill 层 | `d:\Project\skills\case-analysis-document-pack\` | **只读代码与文档**，禁止写运行产物 |
| 工作空间根 | 用户在 WorkBuddy 选择的文件夹 | 只放 `案件库/`、`通用件/` |
| 案件目录 | `<根>/案件库/<YYYYMMDD>-<fyid>-<原告>诉<被告>-<案由>/` | 全部运行产物在此 |

硬规则：session、验证码、日志、OCR 缓存、分析中间产物一律不落 Skill 目录或工作空间根。

## 案件目录内本技能写入的文件

```text
<案件目录>/
  案件分析报告.md
  01原始包/        客户原始聊天导出、压缩包（由 organize_materials.py 归位，只读）
  03图片件/        聊天截图
  04未签稿/        本技能生成的整套可打印签字 Word + 提交材料清单.md
  05案件信息/
    chat_evidence.json
    analysis.json
    analysis_confirm.json
    document_manifest.json
    mcp_status.json
    search_plan.json       # auto_search.py plan 产的检索词与依据
    mcp_calls/             # auto_search.py plan 产的 4 份 MCP 调用参数
    mcp_raw/               # Agent 执行 MCP 后的原始返回（ingest-all 的输入）
  06运行记录/
    OCR 日志、工具反查结果 resolved_tools.json、agent_findings.json（Agent 结构化结论）
```

## 通用模板库

```text
<工作空间根>/通用件/模板/
  01-起诉与答辩/    02-立案通用件/   03-证据/          04-委托代理/
  05-保全与查控/    06-程序性申请/   07-执行/          08-情况说明/
  09-仲裁/          10-专项（劳务）/ 来源登记.json
```

分类与文件名与技能目录 `assets/templates/` 完全同构（副本目录结构一模一样），
逐条对照见 `references/templates-registry.md`。

模板源在技能目录 `assets/templates/`（律师实战模板包，已脱敏），由
`manage_templates.py` **复制**并登记，不下载、不生成骨架。渲染只读，不改动模板原件。

**模板只读**：`通用件/模板/` 下所有模板复制后即 `chmod 444`，所有填充都发生在
`04未签稿/` 的副本上。`--verify` 查改动，`--restore` 恢复，`--unlock` 供删除前解锁。

模板缺失时 `manage_templates.py` / `render_documents.py` 直接报错退出，由 Agent 告知用户补文件，
不允许用自造 docx 顶替。

## 禁止

- 禁止在 Skill 目录、工作空间根、桌面或个人目录写运行产物。
- 禁止改名或删除 `01原始包/` 原件。
- 禁止生成客户确认表 Word、财产线索表。
- 禁止调用 `ensure_login`、上传、提交。
