# 华宇元典法律数据 MCP（yuandian-mcp）

> 本技能采用双源：元典 + 企查查法律数据（见 `mcp-qcc.md`）。至少连一个源才能出法律结论；只连一个源时报告会标“单源检索结果”。

## 前置条件

- 在 WorkBuddy 连接器中连接 `yuandian-mcp`。官方连接器走 OAuth，不需手填 API Key；注册送 4500 积分，每月赠 3500 积分，基本不花钱。
- 未连接时按下文“暂停文案”处理，不输出法律/案例结论。

## 真实工具名（2026-11 实测）

| 用途 | 真实全名 | 曾用名 |
| --- | --- | --- |
| 法条语义检索 | `mcp__yuandian-mcp__yuandian_semantic_search_legal_articles` | `yuandian_law_vector_search` |
| 类案语义检索 | `mcp__yuandian-mcp__yuandian_semantic_search_cases` | `yuandian_case_vector_search` |
| 法规列表检索 | `mcp__yuandian-mcp__yuandian_search_laws_and_regulations` | `yuandian_rh_fg_search` |
| 案例列表检索 | `mcp__yuandian-mcp__yuandian_search_judicial_cases` | `yuandian_rh_ptal_search` |

前缀仍可能随连接器显示名漂移，Agent 先反查一次：

```
ToolSearch(tool_names=[
  "mcp__yuandian-mcp__yuandian_semantic_search_legal_articles",
  "mcp__yuandian-mcp__yuandian_semantic_search_cases"
])
```

反查结果写入 `<案件目录>/06运行记录/resolved_tools.json`：

```json
{"yuandian.law": "<真实全名或 null>", "yuandian.case": "<真实全名或 null>"}
```

再用 `python scripts/yuandian_search.py probe --case-dir <目录> --tools-json <文件>` 校验；退出码 1 表示未连全。

## 调用参数（用新字段名）

法条检索：

```json
{
  "tool": "mcp__yuandian-mcp__yuandian_semantic_search_legal_articles",
  "params": {
    "query": "买卖合同 卖方逾期交货 违约责任",
    "rewrite_enabled": false,
    "limit": 5,
    "law_article_filter": {"validity_status": ["现行有效"]}
  }
}
```

案例检索：

```json
{
  "tool": "mcp__yuandian-mcp__yuandian_semantic_search_cases",
  "params": {
    "query": "买卖合同 逾期交货 继续履行",
    "rewrite_enabled": false,
    "limit": 3,
    "case_document_filter": {
      "decision_date_start": "2021-01-01",
      "case_category": "民事案件",
      "cause_of_action": ["买卖合同纠纷"],
      "province": "浙江"
    }
  }
}
```

参数约束（实测踩过）：

- `limit` 替代旧的 `return_num`；`rewrite_enabled` 替代 `rewrite_flag`；`law_article_filter` / `case_document_filter` 替代 `fatiao_filter` / `wenshu_filter`。
- `case_document_filter.province` **只接受简称**（浙江），传“浙江省”会过滤为空；`yuandian_search.py` 已做映射。
- `document_type` 传编码 `["1"]` 判决书；`yuandian_search_judicial_cases` 的 `document_type` 才用文字。
- `decision_date_start` 固定 `2021-01-01`；案例保留 1–5 个，优先同管辖省份。

## 返回结构（实测）

法条：

```json
{"success": true, "results": {"law_article_list": [
  {"article_id": "...", "article_number": "第六百零五条", "regulation_name": "中华人民共和国民法典",
   "content": "……", "validity_status": "现行有效", "primary_effect_level": "法律",
   "effective_date": 20210101, "url": "https://ydzk.chineselaw.com/..."}
]}}
```

类案：

```json
{"success": true, "results": {"document": [
  {"scid": "...", "title": "...", "case_number": "（2025）浙1022民初1186号",
   "court_name": "三门县人民法院", "province": "浙江", "decision_date": 20250730,
   "case_category": "民事案件", "document_type": "判决书", "content": "...", "url": "..."}
]}}
```

校验要点：

- 返回没有 `code`，用 `success` 判断；`success=false` 直接报错，不静默写空。
- `decision_date` 是 `20250730` 这类数字，`yuandian_search.py` 统一转 `2025-07-30`。
- 列表为空写“未检索到”，不补造案号/法条。

## 未连接暂停文案

> 元典法律检索 MCP（yuandian-mcp）未连接，已暂停法律/案例检索；本报告仅呈现有来源的事实，不输出法律适用与类案结论，且不虚构案号/法条。请先连接 yuandian-mcp 后重跑。

## 禁止

- 禁止用本地记忆补法条、案号、裁判日期。
- 禁止把 `success=true` 但列表为空当查到结果。
- 禁止把未检索核验的推断表述为已核验结论。
- 禁止在本技能内维护本地知识库/案由规则卡；确需领域口径时走检索。
