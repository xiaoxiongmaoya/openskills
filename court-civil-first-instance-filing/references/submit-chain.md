# 民事一审提交链路参考（法院诉讼服务平台）

服务前缀（均相对 `base_url`，默认 `https://zxfw.court.gov.cn`）：

- `/yzw/yzw-zxfw-lafw` — 立案服务（layy = 立案单）
- `/yzw/yzw-zxfw-ajfw` — 案件服务（材料上传签名、案件/立案列表）
- `/yzw/yzw-zxfw-yhfw` — 用户服务（登录、公钥、验证码）

## 1. 提交固定顺序（13 步）

严格按序；任一步失败即中止、记录断点、输出诊断，不跳步重试。

| # | 步骤 | 方法 | 路径 | 产出 |
| --- | --- | --- | --- | --- |
| 1 | 确认受理法院 | GET | `/yzw/yzw-zxfw-lafw/api/v3/pz/fy` | `fyid` |
| 2 | 创建立案草稿 | POST | `/yzw/yzw-zxfw-lafw/api/v3/layy` | `filing_id` |
| 3 | 读取材料模板 | GET | `/yzw/yzw-zxfw-lafw/api/v3/pz/layymb/{fyid}/{ajlx}` | 材料槽位清单 |
| 4 | 案由保存 | PATCH | `/yzw/yzw-zxfw-lafw/api/v3/layy` | 案由编码 |
| 5 | 当事人保存 | POST | `/yzw/yzw-zxfw-lafw/api/v3/layy/dsr` | 原告/被告 |
| 6 | 代理人保存 | POST/PATCH | `/yzw/yzw-zxfw-lafw/api/v3/layy/dlr` | 代理人 |
| 7 | 标的金额保存 | PATCH | `/yzw/yzw-zxfw-lafw/api/v3/layy` | `sqbdje` |
| 8 | 材料上传 | 3 步 | signature → OSS → ssclfj | 材料关联 |
| 9 | 诉请与事实理由 | POST | `/yzw/yzw-zxfw-lafw/api/v3/layy/ssqq` | 诉讼请求 |
| 10 | 先行调解意愿 | GET/PATCH | `pz/tjpz` + `layy.lasfzsqt` | 用户选择是/否 |
| 11 | 草稿有效性复核 | GET | `/api/v3/layy/{filing_id}` | 失效则回第 2 步重建 |
| 12 | 回读确认 | GET | `layy/layyxq/{id}/0` | 用户看真值再点头 |
| 13 | 最终提交 | PATCH | `/yzw/yzw-zxfw-lafw/api/v3/layy/submit` | 提交成功 |

提交前准入自查（缺一不可）：受理法院已确认、草稿有效、必传材料齐备且无未分类文件、诉请与事实理由非空、原告与被告均非空、`dsrMc` 含「原告：/被告：」、已选定先行调解意愿（`lasfzsqt`）、已确认是否上传被告身份证照片（`decisions.defendant_id_photo`）。

## 2. 接口明细

### 2.1 法院与案由

```
GET /yzw/yzw-zxfw-lafw/api/v3/pz/fy?sfid={region_code}&city=&fymc={keyword}
GET /yzw/yzw-zxfw-lafw/api/v3/pz/fy/ajlx/{region_code}     # 区域立案类型树，找"民事一审"入口
```

- `region_code`：省级区域代码，默认 `330000`（浙江），按受理法院所属省份调整。
- 民事一审入口的 `ajlx`（案件类型编码）以类型树返回为准，不要硬编码。
- 兵团法院注意：如"某某垦区"走新疆生产建设兵团链路，法院名为"新疆生产建设兵团某某垦区人民法院"，以搜索返回的 `fyid` 为准。

### 2.2 立案草稿 layy

```
POST /yzw/yzw-zxfw-lafw/api/v3/layy            # 创建
PATCH /yzw/yzw-zxfw-lafw/api/v3/layy           # 局部更新（案由/标的金额）
PATCH /yzw/yzw-zxfw-lafw/api/v3/layy/submit    # 最终提交（完整 layy 对象）
```

创建请求体（首版骨架，字段值随法院模板微调）：

```json
{
  "ajcx": "sp",
  "ajlx": "<民事一审入口返回的 ajlx>",
  "tjcgsqsfqr": "1",
  "fyid": "<fyid>",
  "sqrsf": "11800010-1",
  "ajlb": "sp",
  "pcSqrLx": "",
  "sqrlx": "11800011-2",
  "sfid": "<region_code>",
  "citymc": "",
  "ftmc": "",
  "sfzscq": "1501_000010-2",
  "sfysla": "1501_000010-2",
  "lafs": "2"
}
```

案由保存（PATCH）：`{"id": filing_id, "laay": "民间借贷纠纷", "laayMz": "9181"}`。

**字段语义与直觉相反，务必记牢：`laay` = 案由名称，`laayMz` = 案由树节点 id。** 写成 `laay=002004001031004 / laayMz=民间借贷纠纷` 时接口照样返回 200，但法院前台会把编码原样显示出来（已实测踩坑）。

案由树：`GET /yzw/yzw-zxfw-lafw/api/v1/ay/tree/batch?lbs=0300`（必须 GET + `lbs`；POST 返回 405，缺 `lbs` 返回 400）。节点 `id` → `laayMz`，`fjm` → 编码（前台不用），`name` → `laay`。封装：`client.save_cause(filing_id, "民间借贷纠纷")`。

标的金额（PATCH）：`{"id": filing_id, "sqbdje": "<金额字符串>"}`。

⚠️ `PATCH /api/v3/layy` 是**全量覆盖**：只传 `sqbdje` 会把 `laay`/`laayMz`/`ajmc` 清成 null。业务写入统一走 `client.patch_layy({...})`（先 GET 详情合并，再整对象 PATCH）。

**最终提交体是完整 layy 对象**（提交前先 GET 草稿详情，回填最新字段再 PATCH），只传 `id` 会被法院拒绝。

### 2.3 当事人 dsr

```
GET    /yzw/yzw-zxfw-lafw/api/v3/layy/dsr/list/{filing_id}/{ajlx}   # 列远端当事人
POST   /yzw/yzw-zxfw-lafw/api/v3/layy/dsr                           # 新增
DELETE /yzw/yzw-zxfw-lafw/api/v3/layy/dsr/{filing_id}/{party_id}    # 删除
```

保存策略：先列远端旧当事人并全部删除，再逐条新增，保证远端与本地快照一致（幂等重建）。

当事人 payload 要点：`layyid`、`fyId`、`ajlx`、角色（原告/被告）、姓名、证件类型（`zjlx`，居民身份证 = `1501_000015-1`）、证件号、民族、住所地；组织当事人（公司）走组织字段分支（统一社会信用代码、法定代表人）。证件号可反推出生日期/性别/民族字段，减少手工录入。

建草稿后系统会**自动生成一条当事人**：`xm`=登录律师本人、`ssdw`=`1501_030109-1`（原告）、`sfsqr`=是。必须连同其它旧当事人一起删掉，再写真实原告/被告。

**`layy.dsrMc` 必须一起写**：它是前台「参与人」栏的展示串，由前端拼好随 layy 提交，格式 `原告：A，B；被告：C`（中文逗号分隔多人、分号分隔角色）。只写 `dsr` 不写 `dsrMc`，前台就只显示名字、没有原被告。写完后调 `client.sync_dsr_mc(filing_id)` 重算 `dsrMc` 与 `ajmc`（`X诉Y案由一案`）。

码表（`GET /yzw/yzw-zxfw-lafw/api/v1/cyzl/code?codeType=`）：
`000011` 当事人类型（自然人 `1501_000011-1`）、`000013` 代理人类型（执业律师 `1501_000013-1`）、`030109` 诉讼地位（原告 `-1`/被告 `-2`）、`GB0001` 性别、`GB0006` 国别（中国 `1501_GB0006-156`）、`000015` 证件类型、`000010` 是/否。

### 2.4 代理人 dlr

```
POST   /yzw/yzw-zxfw-lafw/api/v3/layy/dlr                            # 新增
PATCH  /yzw/yzw-zxfw-lafw/api/v3/layy/dlr                            # 更新（带远端 id）
DELETE /yzw/yzw-zxfw-lafw/api/v3/layy/dlr/{layyid}/{id}              # 删除
```

payload 要点：`layyid`、`fyId`、`dlrlx`（代理人类型编码）、`cdlrlx`（子类型）、`xm`、`zjlx`、`zjhm`、`sjhm`、`zyjg`（执业机构）、`zyzh`（执业证号）、`bdlrMc`（被代理人姓名）。

- `dlrlx`/`cdlrlx` 必须是法院确认过的真实编码，拿不到就停下来问用户，禁止编造。
- 律师用户（`lsyh`）先拉草稿详情里的远端代理人候选，按姓名+证件号匹配：命中则 PATCH 更新，未命中才 POST 新增。
- PATCH 时需回带远端记录的 `gj`（国籍）、`cgj`、`sfsqr` 等字段，不要只传本地字段。

### 2.5 材料上传（每个文件三步）

```
POST /yzw/yzw-zxfw-ajfw/api/v1/file/upload/signature    # 1 取签名
POST {ossPath}                                          # 2 直传 OSS（表单）
POST /yzw/yzw-zxfw-lafw/api/v3/layy/ssclfj              # 3 关联材料槽位
```

签名请求体：

```json
{"path": "layy", "ext": ".pdf", "fydm": "<fyid>", "cllx": "<法院材料类型编码>"}
```

签名响应取：`ossPath`（上传地址）、`storeAs`（对象键）、`policy`、`OSSAccessKeyId`、`signature`、`token`（sts）。

OSS 直传表单：`key=storeAs`、`policy`、`OSSAccessKeyId`、`success_action_status=200`、`signature`、`x-oss-security-token=token`，文件以 multipart `file` 字段 POST 到 `ossPath`。

材料关联请求体：

```json
{
  "wjbh": "<storeAs>",
  "layyid": "<filing_id>",
  "fyId": "<fyid>",
  "wjmc": "<文件名>",
  "path": "<storeAs>",
  "ssclid": "<法院材料模板槽位 ID>",
  "cllx": "<法院材料类型编码>",
  "clmc": "<材料名称>",
  "bccl": 1,
  "name": "<文件名>",
  "extname": "pdf",
  "url": "<签名返回的 url/previewUrl/storeAs>",
  "xh": 1
}
```

**两个必踩的坑：**

1. `ssclid` 必须用**草稿详情** `data.sscls[].id`（每单生成的 uuid），不是模板 `sscl[].id`（"1"~"7"）。
2. **模板里有槽位 ≠ 前台可见**：账户/收款确认书只传到 `11800016-36` 时接口 200、文件也查得到，但法院前台材料清单里看不到，审核侧按「其他材料」口径检查。必须再传一份到 `11800016-3`，命名如 `其他材料-账户确认书.pdf`。

其他材料接口：

```
GET    /yzw/yzw-zxfw-lafw/api/v3/layy/ssclfj/list/{filing_id}/{slot_id}   # 槽位下已传文件
POST   /yzw/yzw-zxfw-lafw/api/v3/layy/ssclfj/delete                      # 删除（前端把 ywcs.id 改写为文件 id）
```

### 2.6 诉请 ssqq

```
POST /yzw/yzw-zxfw-lafw/api/v3/layy/ssqq
```

承载诉讼请求条目 + 事实与理由文本。提交前本地校验：诉请文本、事实理由文本均非空，否则法院端"起诉状不能为空"类报错会在最终提交才暴露。

### 2.7 提交配置、先行调解与排期

```
GET /yzw/yzw-zxfw-lafw/api/v3/pz/tjpz?fyid={fyid}                          # 提交配置（调解告知）
GET /yzw/yzw-zxfw-lafw/api/v3/layy/sjzt/ajpqtj/count/{fyid}                # 该院排期在前的案件数
```

`tjpz` 关键字段：`sfkt`（是否走「先行调解告知」流程）、`title`（弹窗标题，如"网上立案环节先行调解告知内容"）、`tsy`（告知正文，含调解期限/费用减免等，**原文转给用户，不要自行编写**）、`tzUrl`、`djfs`。

排期数（某区法院实测 N 件）用于前台原话：
`您好，您将向{fymc}提交立案申请，在您的案件之前有{N}件案件正在排期等待开庭审理。请问您是否考虑选择先行调解？`

用户选择写入 `layy.lasfzsqt`：`1501_000010-1` 同意 / `1501_000010-2` 不同意。
**这一步必须由用户决定**，Agent 不得代答；封装见 `get_mediation_notice()` / `mediation_code()`。
`lasfzsqt` 单独 PATCH 不落库，必须在 `submit` 的完整对象里带上。

### 2.8 状态与列表

```
GET  /yzw/yzw-zxfw-lafw/api/v3/layy/layyxq/{filing_id}/0    # 立案单详情（按 id 查状态，首选）
GET  /yzw/yzw-zxfw-lafw/api/v3/layy?page=1&limit=20        # 立案单列表（含草稿/已提交），参数是 page+limit
GET  /yzw/yzw-zxfw-ajfw/api/v1/ajlist                      # 案件列表（POST 亦可，body 带分页与筛选）
GET  /yzw/yzw-zxfw-sxfw/api/v1/user/info                   # 登录用户（律师执业证号 cLszyzh）
```

- **`filing_id`（layy id）提交后不变，作为长期查询钥匙**。`get_filing_status(filing_id)` 一次返回状态文本、提交时间、案号、审核意见、当事人角色、代理人、材料清单。
- 详情里状态字段：`zt` 状态码、`tjsj` 提交时间、`ah`/`spajah` 案号、`shyj` 审核意见、`lasfzsqt` 调解意愿。
- 列表返回 `laay`=案由名称、`laayMz`=节点 id，可用来核对是否写反。
- 刚提交的立案单**不在** `ajlist`（案件列表）里，要查 `layy` 详情或列表。
- `tjajList` 在本环境 500（`Table 'yzwzxfw_ajfw.t_tjaj_zy' doesn't exist`），不要依赖。
- `page`/`limit` 传成 `pageSize`/`size` 会 400（`page:不能为null` / `limit:不能为null`）。

立案状态码：`11800007-1` 待审核 / `-2` 审核通过 / `-3` 审核未通过 / `-4` 已立案 / `-6` 补充材料。轮询间隔 ≥ 1 分钟，仅在用户要求同步时执行。

## 3. 失败处理

| 现象 | 诊断动作 | 处理 |
| --- | --- | --- |
| 草稿详情拉取失败且提示失效 | `_check_remote_filing_valid` | 自动重建草稿（回第 2 步）并重跑后续步骤；重建仍失败则中止交人工 |
| 最终提交报"起诉状不能为空" | `POST /api/v3/layy/ssclfj/actions/zzqsz`（body `{"layyid": filing_id}`） | 检查材料槽位映射、诉状上传记录与草稿一致性；诊断含"请选择签名文件"时优先补签名材料 |
| 当事人/代理人保存 4xx | 查看 message | `dlrlx`/`cdlrlx` 等编码缺失时停下来问用户，禁止编造编码重试 |
| 材料 OSS 直传失败 | 看 HTTP 状态 | 重新取签名（签名一次性）再传；≤ 2 次 |
| 登录 401 / 会话失效 | — | 清持久化会话，重走登录四步 |

## 4. 已知坑

完整清单见 `known-issues.md`，这里只留最容易踩的四条：

1. `laay`=案由名称、`laayMz`=节点 id，写反 → 前台显示编码串。
2. `PATCH /api/v3/layy` 全量覆盖 → 一律用 `patch_layy()`。
3. 材料 `ssclid` 用详情 `sscls[].id`（uuid）；账户确认书要额外进「其他材料」槽。
4. `submit` 是 PATCH 且必须带完整 layy 对象；提交前先 GET 详情。

其余：
- `submit` 是 PATCH 且必须带完整 layy 对象；先 GET 草稿详情再提交。
- 代理人 PATCH 必须回带 `gj`/`cgj`/`sfsqr` 等远端字段，漏带会导致字段被清空。
- 删除材料的前端实现是把当前文件 `id` 写进 `ywcs.id` 再 POST 到删除地址，直接复刻该行为。
- 验证码 `id` 一次性，重试必须重新获取。
- 各法院模板字段存在差异，模板接口（`pz/layymb`）返回是唯一事实源，按模板驱动表单而不是硬编码字段清单。
