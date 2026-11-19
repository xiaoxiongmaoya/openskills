# 企查查·法律数据 MCP（qcc-legal）

## 前置条件

- 连接器：企查查法律数据（`qcc-legal`）。实测已连接可用。
- **WorkBuddy 连接器里的“企查查”默认只连 `qcc-company`，不含法律数据。** 若只连了市场版且没有法律工具，需在 [agent.qcc.com](https://agent.qcc.com) 获取 API Key 自定义配置。
- 费用：注册赠 500 积分（30 天），每日赠 100 积分（普通用户），1 元 = 10 积分；不同工具单价不同。

## 真实工具名（实测）

真实 server 前缀就是 `mcp__qcc-legal__`，不是 `qcc-legal-regulation` / `qcc-legal-case`：

| 工具全名 | 用途 | 关键参数 |
| --- | --- | --- |
| `mcp__qcc-legal__get_legal_article_semantic_search` | 法规语义检索，问题式/案情式首选 | `query`（完整自然语言问题）、`effectStatus` |
| `mcp__qcc-legal__get_legal_regulation_search` | 法规关键词检索 | `keyword`、效力级别、时效性 |
| `mcp__qcc-legal__get_legal_article_search` | 法条字面关键词检索 | `keyword` |
| `mcp__qcc-legal__get_legal_article_detail` | 法条逐字原文 | `id` 或 `regulationName` + `articleNo` |
| `mcp__qcc-legal__get_legal_regulation_detail` | 整部法规全文 | `id` 或法规名 |
| `mcp__qcc-legal__get_legal_citation_verify` | 法条引用回库核验 | `text`（≤5 万字） |
| `mcp__qcc-legal__get_judicial_case_search` | 普通裁判文书检索 | `keyword`、`caseReason[]`、`province[]`、`docType[]`、`judgeDateGte` |
| `mcp__qcc-legal__get_judicial_authoritative_case_search` | 权威案例检索 | `keyword`、案例来源 |
| `mcp__qcc-legal__get_judicial_case_detail` | 案例全文 | `id` 或完整案号 |
| `mcp__qcc-legal__get_judicial_citation_verify` | 案号引用回库核验 | `text`（≤5 万字） |

## 返回结构（中文键，实测）

法条：

```json
{"摘要": "共检索到 222 条相关的法条", "法条列表": [
  {"法条ID": "rid1.xxx", "所属法规名称": "中华人民共和国民法典", "条号": "第五百七十七条",
   "条文正文": "……", "时效性": "现行有效", "效力级别一级": "法律",
   "引用链接": "https://www.qcclegal.com/regulation/...", "相关性得分": 100}
]}
```

案例：

```json
{"摘要": "共检索到 79 条", "案例列表": [
  {"案例ID": "cid1.xxx", "案号": "（2025）浙0000民初1号", "案件标题": "……",
   "法院名称": "浙江省杭州市钱塘区人民法院", "法院级别": "基层人民法院", "省份": "浙江",
   "案由": "买卖合同纠纷", "案件类别": "民事案件", "文书种类": "判决书", "审级": "一审",
   "裁判日期": "2025-07-23", "案例详情页链接": "https://www.qcclegal.com/case/...",
   "案例摘要": "## 基本事实\n…## 本院认为\n…## 争议焦点\n…## 裁判结果\n…"}
]}
```

`yuandian_search.py` 已同时兼容元典英文键与企查查中文键。

## 工具反查

Agent 先 `ToolSearch` 反查真实全名，把结果写入 `<案件目录>/06运行记录/resolved_tools.json`：

```json
{
  "yuandian.law": "mcp__yuandian-mcp__yuandian_semantic_search_legal_articles",
  "yuandian.case": "mcp__yuandian-mcp__yuandian_semantic_search_cases",
  "qcc.law": "mcp__qcc-legal__get_legal_article_semantic_search",
  "qcc.case": "mcp__qcc-legal__get_judicial_case_search"
}
```

`probe` 接受 `qcc.law` / `qcc.case` 逻辑键；前缀若再变，以 ToolSearch 返回为准。

## 调用约束

- 法条语义检索：`query` 直接传完整问题，不要压成关键词；加 `effectStatus: ["现行有效"]`。
- 普通案例：`keyword` 传案情关键词，配合 `caseReason`、`province`（数组、简称）、`docType`、`judgeDateGte`；权威案例另用 `get_judicial_authoritative_case_search`。
- 检索结果是摘要/元信息，逐字引用前必须调详情工具取原文。
- 援引前核对 `effectStatus` 为“现行有效”。
- 引用核验工具只回库校验已有引用，不用它扩充内容。
- 未命中就写“未检索到”，不编造。

## 双源交叉

`yuandian_search.py ingest` 会把元典与企查查结果分别写入 `analysis.json` 的 `law_search.providers` / `case_search.providers`，并生成：

- `results`：合并去重后的前 5 条；
- `cross_check.both_returned`：两源是否都返回；
- `cross_check.conflicts`：同一法条内容不一致、同一案号法院不一致的冲突项。

冲突项必须在报告中提示律师人工复核，不能自动择一。
