---
name: court-civil-first-instance-filing

display_name: "法院民事一审在线立案 + 强制执行申请"

display_name_en: "Court Civil Filing & Enforcement Application"

description: "登录法院诉讼服务平台并完成民事一审立案与强制执行申请全链路（登录、建草稿、案由或执行依据/原审信息、当事人/代理人、标的额、材料上传、诉请或执行请求、提交、状态查询）。当用户要求登录法院账号、在线立案、民事一审、申请强制执行、提交立案材料、查询立案状态时使用本技能。"

description_zh: "登录法院诉讼服务平台（默认 zxfw.court.gov.cn，兼容同构各地法院网），完成民事一审立案与强制执行申请（case_type=zx）全链路：SM2 登录、建草稿、匹配法院、案由或执行依据/原审信息、当事人/代理人/标的额、材料上传（签名→OSS→槽位）、诉请或执行请求、人工确认后提交、按 id 查状态。凭证零硬编码。"

description_en: "Log in to court litigation platforms and complete civil first-instance filing and enforcement application: SM2 login, draft, court match, cause or enforcement basis, parties/agent/amount, material upload, claims or enforcement requests, confirm-then-submit, status query by filing id."

category: "writing"

version: "1.5.2"

author: "court-assistant"

examples_zh:
- "登录我的法院诉讼服务平台账号，准备立一个民事一审案"
- "帮我起草一起民间借贷纠纷的民事一审立案材料"
- "凭生效判决书申请强制执行"
- "查询我刚提交的立案申请当前状态"
examples_en:
- "Log in to my court litigation service account to prepare a civil first-instance filing"
- "Draft civil first-instance filing materials for a loan dispute"
- "File an enforcement application based on an effective judgment"
- "Query the current status of my just-submitted filing"
---

# 法院民事一审在线立案 + 强制执行申请

默认平台 `https://zxfw.court.gov.cn`；同族接口（`/yzw/yzw-zxfw-*`）换 `base_url` 即可复用。

**适用**：登录法院、在线立案、民事一审、**申请强制执行**、提交材料、查立案状态。

**不适用**：非 `yzw-zxfw-*` 接口族的法院平台（先抓包确认接口族）。

**两类案件**：`case_type=sp` 民事一审（默认）/ `case_type=zx` 强制执行申请。执行立案与一审
共用同一套 `layy` 接口，差异见 @references/execute-chain.md。

**两种账号**：`user_type=lsyh` 律师（默认）/ `gryh` 个人 / `fryh` 法人。个人账号本人立案时
不建代理人、不查律师证。

## 快速开始

```bash
# 0 依赖（一次即可；onnxruntime 必须钉 1.20.1，见 known-issues #10）
pip install requests gmssl pillow ddddocr "onnxruntime==1.20.1" coloredlogs sympy

# 0.5 环境自检（升级任何依赖后先跑这个）
python scripts/ocr_probe.py

# 1 建案件目录（中文骨架，幂等）
python scripts/bootstrap_case.py --root "<工作空间根>" --fyid 0000 \
  --court "某某市某某区人民法院" --region 000000 \
  --cause "民间借贷纠纷" --plaintiff "张三" --defendant "李四"

# 2 整理客户材料（GBK 转码 + 分类 + 规范命名，只复制不删除；复制走线程池）
python scripts/organize_materials.py --case-dir "<案件目录>" --source "<客户zip>"

# 3 离线门禁（不联网、不建档）：材料没通过就别往下走
python scripts/preflight_materials.py --case-dir "<案件目录>"

# 4 填 spec 后试跑（默认 dry-run，不碰法院；会先过一遍门禁）
python scripts/filing_runner.py "<案件目录>/05案件信息/spec.json"

# 5 分步执行（推荐）：只建草稿，filing_id 会回写 spec，后面复跑不会重复建档
python scripts/filing_runner.py "<案件目录>/05案件信息/spec.json" --apply --stage create
python scripts/filing_runner.py "<案件目录>/05案件信息/spec.json" --apply --stage upload

# 6 回读确认（贴给用户看的唯一依据）
python scripts/confirm_filing.py --case-dir "<案件目录>"

# 7 用户点头后提交（材料已传完时用 submit，不重传）
python scripts/filing_runner.py "<案件目录>/05案件信息/spec.json" --apply \
  --stage submit --mediation no --defendant-id-photo skip --submit --yes

# 8 多案批量（默认单线程；上传永远串行，见下文「上传节流」）
python scripts/run_batch.py --root "<工作空间根>" --stage upload

# 9 日常轮询：待审核 + 审核通过等案号（有案号的不再查）
python scripts/poll_pending.py --root "<工作空间根>"            # 先看要查哪些
python scripts/poll_pending.py --root "<工作空间根>" --apply    # 真查 + 回写

# 10 单案查（任何状态都适用）
python scripts/query_filing.py --case-dir "<案件目录>" --write
```

### 多原告 / 多被告

一个案子有多个当事人时，`spec.json` 用**列表字段**（runner 已支持，旧单数写法继续兼容）：

- `plaintiffs`: `[{"name": "...", ...}, ...]`（多原告）
- `defendants`: `[{"name": "...", ...}, ...]`（多被告）
- 只有一个时仍可写单数 `plaintiff` / `defendant`
- `bootstrap_case.py` 直接用 `、` 或 `,` 分隔姓名即可自动拆成列表：`--defendant "蔡炳阶、余金花"`

fill 阶段逐个 `POST dsr`（原告在前、被告在后），再 `sync_dsr_mc()` 生成 `原告：A；被告：B，C`；
`preflight_materials.py` / `write_case_note.py` 同样识别列表，说明文档会显示全部当事人。

## 强制执行申请（case_type=zx）

同一套 runner，加 `--case-type zx`。与一审的差异（实测，详见 @references/execute-chain.md）：

| 项 | 民事一审 | 强制执行 |
| --- | --- | --- |
| 建单 `ajcx`/`ajlb` | `sp` | `zx` |
| `ajlx` | `sp` | `1501_000001-1002` |
| `lafs` | `"2"` | 不传 |
| 案由 `laay` | 必填名称+节点 id | **没有案由树**，恒为 null |
| 标的额 `sqbdje` | 必填 | 不适用 |
| 执行依据 | — | `PATCH /api/v3/zxyj` |
| 原审信息 | — | `PATCH /api/v3/layy/ysxx` |
| 原审案号候选 | — | `POST /yzw/yzw-zxfw-ajfw/api/v1/ysajlist` |
| 先行调解 | 必问 | **不适用** |
| 材料槽 | 起诉状/身份/证据/送达/账户 | 执行申请书/执行依据文书/申请人身份/委托书/送达/收款账户 |
| 诉讼地位 | 原告 `030109-1` / 被告 `-2` | 申请执行人 `100225-1` / 被执行人 `-2` |
| 案名 `ajmc` | 程序自动拼 `X诉Y案由一案` | **不会自动生成，必须拼**（空 → 提交报 `RMFY109905`） |
| 执行请求/理由 | `ssqq` / `ssly` | `zxyjs[0].zxqq` / `.zxyjzw`（**不走 ssqq**） |
| 执行标的 | — | `zxyjs[0].sqzxbdzl` + `.sqzxbdje` |

⚠️ 执行案件提交失败只看三件事：`ajmc` 非空、`zxyjs[0]` 的 `zxqq`/`zxyjzw`/`sqzxbdzl` 齐、
申请执行人 `hjszd` 非空。其余都不是原因。

```bash
# 建执行案件骨架（case_type=zx 时 --cause 可省）
python scripts/bootstrap_case.py --root "<工作空间根>" --fyid 1300 \
  --court "某某市某某区人民法院" --region 330000 \
  --plaintiff "张三" --defendant "李四" --case-type zx

# 查原审案号候选（拿 ysajbh / ysajAjbs / cfydm）
python -c "from court_client import CourtClient as C; import json; \
c=C(); c.ensure_login(); print(json.dumps(c.list_ysaj(), ensure_ascii=False, indent=2))"

# 填 spec.zxyj（lb/ah/jbjg/jbjgMc/ysajbh/ysajAjbs/ysajah）后试跑
python scripts/filing_runner.py "<案件目录>/05案件信息/spec.json"            # dry-run
python scripts/filing_runner.py "<案件目录>/05案件信息/spec.json" --apply --stage create
python scripts/filing_runner.py "<案件目录>/05案件信息/spec.json" --apply --stage fill

# 回读确认 + 提交（执行案件无先行调解，可不给 --mediation）
python scripts/confirm_filing.py --case-dir "<案件目录>" --case-type zx
python scripts/filing_runner.py "<案件目录>/05案件信息/spec.json" --apply \
  --stage submit --defendant-id-photo skip --submit --yes
```

个人账号登录：

```bash
python scripts/login_captcha.py fetch --case-dir "<案件目录>" --user-type gryh
python scripts/login_captcha.py login --case-dir "<案件目录>" --code XXXX --user-type gryh
```

`spec.json` 里也可以直接写 `"case_type": "zx"` 和 `"user_type": "gryh"`，CLI 不传就按 spec 走。

## 两条硬规则

1. **确认 = 回读法院**。给用户确认的每个字段必须来自 `layy/layyxq` 返回值（`confirm_filing.py`），禁止用本地 spec 或入参渲染。本地意图和远端真值之间，恰好隔着"字段写反""PATCH 覆盖"这类错误。
2. **两项必问，缺一即中止**：`mediation`（是否同意先行调解）、`defendant_id_photo`（是否上传被告身份证照片）。身份信息（证件号）可以照填，**照片必须单独确认**。

## 运行机制（决定快慢的三件事）

- **登录态全局共用**：session 落在 `<工作空间根>/通用件/.court_session.json`（可用 `COURT_SESSION_FILE` 覆盖）。token 1 小时有效，多案只登录一次，不再每案一次验证码。写入是原子的，401 只清内存不删文件，不会把别的 worker 一起登出。
- **限速是账号级的**：`--rate`（默认 1.0 次/秒）由跨进程令牌桶统一控制，不是每个进程各睡各的。调高会放大法院风控风险，属于用户决策。
- **远端数据只读一遍**：单次运行内 detail / 槽位文件列表 / 当事人 / 排队数 / 案由树都有缓存，写操作后就地合并。以前提交前那段要 40+ 次请求，现在 12～15 次。
- **建档幂等**：`filing_id` 建完就回写 spec；复跑走"复用草稿"分支，不会新建第二张单、也不会重传材料。

### 上传节流（这段别乱调，是踩过 403 换来的）

上传走**独立慢通道**，和普通接口限速分开算：

| 开关 | 默认 | 说明 |
| --- | --- | --- |
| `--upload-interval` | 3.5s | 每份材料之间的最小间隔 |
| `--upload-jitter` | 1.5s | 间隔上叠加随机抖动（固定节拍最像机器） |
| `--upload-batch` | 6 | 每传 N 份做一次长停，`0` 关闭 |
| `--upload-cooldown` | 25s | 批间长停时长 |

实现上是 `<根>/通用件/.court_upload.state` 一把跨进程文件锁：**签名 → OSS 直传 → 关联槽位**三步整段持锁，
同一时刻全机器只有一个进程在上传。所以 `run_batch.py --workers` 调大也不会并发上传，
只是能并行跑「填字段」这类普通请求。以前多线程各传各的，是 `OSS 403 / 获取上传信息失败` 的主因。

环境变量同名可覆盖：`COURT_UPLOAD_INTERVAL` / `COURT_UPLOAD_JITTER` / `COURT_UPLOAD_BATCH` / `COURT_UPLOAD_COOLDOWN`。
失败也占位（间隔照算），不会因为连续失败变成高频重试。

### 轮询范围：待审核 + 等案号

`poll_pending.py` 默认查两类，其余跳过：

| 状态 | 查不查 | 原因 |
| --- | --- | --- |
| `待审核` | ✅ 查 | 放着不管就会变，唯一必须盯的入口状态 |
| `审核通过` | ✅ 查 | 已批但还没出案号，不查就永远不知道案号什么时候下来 |
| `已立案` 且有案号 | ❌ 不查 | 结果已定 |
| `补充材料` / `审核未通过` | ❌ 不查 | 等人工返工，轮询推不动 |

判据是**有没有案号**：`case_no` 一旦有值，无论状态是什么都不再查（案号到手就是终点）。
所以 `审核通过` 会自动一路查到出案号、转 `已立案`，然后自动退出轮询范围。

要临时扩范围：`--status 待审核 --status 补充材料`。

**驳回后人工「复制立案」的情况**：法院端会多一张新单（新 layy id），本地 `case.json` 仍指向旧单，
轮询看到的永远是旧单的「审核未通过」。这时需要人工把新 id 写进本地：

```bash
python scripts/bind_filing.py --case-dir "<案件目录>" --new-filing-id <新 layy id> --reason "驳回后复制立案"
```

`bind_filing.py` 里保留了 `filings[]` 历史链（旧单的意见/案号不会丢），
但**不做自动发现**——法院端列表接口的排序不可靠（页内乱序，实测第 1 页 71 处逆序断点），
自动认领误配风险高，宁可人工指定一次。

## 目录规范

开工前先读 @references/workspace-layout.md。一句话版本：

```
<工作空间根>/案件库/<YYYYMMDD>-<fyid>-<原告>诉<被告>-<案由>/
  01原始包/ 02签字件/ 03图片件/ 04未签稿/ 05案件信息/ 06运行记录/ 案件说明.md
<工作空间根>/通用件/
```

所有运行产物（session、验证码、日志、spec）都在案件目录内，**不落 skill 目录、不落工作空间根**。一案一 session，禁止跨案复用。

## Workflow

### Phase 1 准备

1. `bootstrap_case.py` 建骨架 → `organize_materials.py` 整材料（输出 `unrecognized` 需人工归位）。
2. `preflight_materials.py` 离线门禁：槽位齐备 / 律师证在 / Word 源在 / 文件存在 / 格式合法。**门禁不过不许建档**——建档后再发现材料错，要么重建单、要么重传一轮（一案 80+ 次请求白跑）。`filing_runner` 会默认先跑门禁，`--skip-preflight` 才能绕过。
2. 收集参数：`base_url`、账号、密码、用户类型（律师 = `lsyh`）。
   **文书信息一律 Word 优先（`read_office_text.py`），不拿 PDF 当数据源**：扫描件 OCR 会把 `200000` 读成 `20000`、把「李明」读成「李朋」。Word 原稿必须留在 `04未签稿/`，整理时不许丢。
   两类案件同一条规则，差别只在缺 Word 时：**一审直接阻断**（起诉状要按 Word 生成）；
   **执行降级放行但打警告**（必备材料本身就是判决书 PDF/签字扫描件），降级时必须逐项人工核对。
3. **诉状没写受理法院时**（要素式诉状常见），按管辖规则给候选再让用户选：

```bash
python scripts/suggest_court.py --case-dir "<案件目录>"     # 输出候选法院 + 依据 + fyid
```

   优先级：文书中明确的法院 → 被告住所地（一般地域管辖）→ 合同履行地（民间借贷为接收货币一方所在地＝原告住所地）→ 级别管辖按标的额校验。**管辖是法律判断，必须用户确认后才能建档。**
4. 依赖：`requests`、`gmssl`、`Pillow`、`olefile`（读 .doc）、`ddddocr` + `onnxruntime==1.20.1`（**版本必须钉死**：1.22+ 在本机 Win10 19045 上 `onnxruntime.dll` DllMain 失败，报 WinError 1114）。

### Phase 2 登录（细节见 @references/login-flow.md）

四步固定顺序：

1. `GET /api/v1/publicKey` 取 SM2 公钥（可能带 `04` 前缀）。
2. `GET /api/v1/captcha` 取 `id` + base64 图片。
3. 识别验证码（4 位）；OCR 不可用则落盘图片人工识别。
4. SM2 加密密码后 `POST /api/v1/login`，体 `{login, pass, yzm, id, type}`。

会话要点：token 优先取响应头 → `data.token`；JWT 解 `exp` 留 3 分钟缓冲（实测有效期 1 小时，过期会自动清空 session 文件）；会话文件统一存 `<工作空间根>/通用件/.court_session.json`（账号级共享，**不是**每案一份，`session_file_for()` 自动算，可用 `COURT_SESSION_FILE` 覆盖）。

非交互登录（脚本/Agent 环境）：

```bash
python scripts/login_captcha.py fetch --case-dir "<案件目录>"          # 落盘 captcha.png
python scripts/login_captcha.py login --case-dir "<案件目录>" --code XXXX
```

### Phase 3 提交链（API 明细见 @references/submit-chain.md）

按序执行，失败即中止并记录断点：

| #  | 动作                            | 要点                                                        |
| -- | ----------------------------- | --------------------------------------------------------- |
| 1  | 确认法院 `pz/fy`                  | 按 `sfid` + `fymc` 关键词得 `fyid`                             |
| 2  | 建草稿 `POST layy`               | 得 `filing_id`（即立案单 id）                                    |
| 3  | 读模板 `pz/layymb/{fyid}/{ajlx}` | 槽位与编码以模板为准                                                |
| 4  | 案由                            | `laay`=名称、`laayMz`=节点 id，**别写反**；用 `save_cause()`         |
| 5  | 当事人                           | 先删系统自动塞的"律师本人"假原告，再逐条 `POST dsr`；写完 `sync_dsr_mc()`       |
| 6  | 代理人                           | 执业律师 `1501_000013-1`，`bdlrid` 指向原告；**律师证必传**（模板写非必传，实务必传） |
| 7  | 标的额                           | `sqbdje`                                                  |
| 8  | 材料                            | 签名 → OSS 直传 → `ssclfj` 关联；`ssclid` 用详情 `sscls[].id`（uuid） |
| 9  | 诉请                            | `POST ssqq`，诉请与事实理由都不能空                                   |
| 10 | 两项必问 + 回读确认                   | 见下方"提交前"                                                  |
| 11 | 提交                            | `PATCH layy/submit`，**完整 layy 对象**                        |

**提交前（缺一不可）**：

```bash
python scripts/confirm_filing.py --case-dir "<案件目录>"
```

- 逐项判定：案由名称/id 是否配对、`dsrMc` 是否含「原告：/被告：」、必传槽是否齐、两项决策是否落值。错项标 `[!!]` 并给正确对照值。
- `client.precheck(filing_id, decisions)` 返回非空即中止；`client.patch_layy()` 写入（内部合并，避免全量覆盖清空字段）。

### Phase 4 状态跟踪

```bash
python scripts/poll_pending.py --root "<工作空间根>" --apply      # 日常：待审核 + 等案号，批量回写
python scripts/query_filing.py --case-dir "<案件目录>" --write     # 单案：任何状态都能查
python scripts/query_filing.py --filing-id <id> --materials       # 单点查
python scripts/query_filing.py --case-dir "<案件目录>" --write --json-only   # 只要机器可读
```

- 轮询默认查 **`待审核` + `审核通过`（等案号）**：补充材料、审核未通过不查，有案号的一律不查（见上文「轮询范围」）。
- `filing_id` 提交后不变，**长期作查询钥匙**；底层 `GET layy/layyxq/{id}/0`。
  驳回后人工复制立案会换新 id，用 `bind_filing.py --new-filing-id` 手工绑一次，
  旧单保留在 `filings[]` 里（不会被覆盖）。
- 状态码：`11800007-100` 草稿 / `-1` 待审核 / `-2` 审核通过 / `-3` 审核未通过 / `-4` 已立案 / `-6` 补充材料。
- **审核意见在 `data.shjgs[]`，不在 `layy` 里**（`layy.shyj` 恒为空，别再去那里找）：
  每条记录含 `cshjg`（审查不通过/退回补充材料/…）、`shyj`（意见全文）、`shsj`（审核时间）、
  `shrxm`（审核人）、`cbhyy`（驳回原因）、`thfjwjs`（退回附件，带签名 URL）。
  脚本已抽取为 `review_records` / `review_opinion` / `review_result` / `review_at` / `reviewer` /
  `reject_reason` / `returned_files`，并在 `--write` 时把退回附件下载到 `05案件信息/法院退回件/`。
- 输出**先人读摘要再 JSON**：审核意见逐行照抄、分段保留，直接转给用户即可，不需要再登网站。
  只要机器可读加 `--json-only`。`--no-download` 跳过附件下载。
- 轮询间隔 ≥ 1 分钟，只在用户要求同步时跑，不自己起定时任务。
- 状态未变但法院新写了审核意见时，`状态变更.log` 仍会补记一行（按 `shjgs[].id` 去重）。
- **给用户看的是 `案件说明.md`**：`--write` 会自动重新生成——状态、单号、提交时间、法院排期数、标的额、决策、材料份数、进度勾选、**法院审核结果全文**全在里面。`.json` 只是机器状态，不要直接丢给用户。

## 脚本

| 脚本                      | 作用                    | 关键参数                                                                                 |
| ----------------------- | --------------------- | ------------------------------------------------------------------------------------ |
| `bootstrap_case.py`     | 建案件中文目录骨架，幂等          | `--root --fyid --court --cause --plaintiff --defendant`                              |
| `login_captcha.py`      | 非交互登录：截图验证码 → 识别 → 登录 | `fetch` / `login --code XXXX` / `status`，均需 `--case-dir`                             |
| `organize_materials.py` | 解压乱包、GBK 转码、分类改名，只复制（两阶段 + 线程池） | `--case-dir --source [--dry-run] [--workers 6]`                                      |
| `preflight_materials.py` | 离线门禁（不联网、不建档）        | `--case-dir` `[--json]` `[--allow-missing-source]`                                    |
| `ocr_probe.py`          | 验证码 OCR 环境自检          | `[--image captcha.png]`                                                              |
| `run_batch.py`          | 多案批量（子进程隔离 + 全局令牌桶，上传串行）   | `--root --stage upload --rate 1.0` `[--workers N]` `[--upload-interval 3.5]` `[--submit --yes]` |
| `bind_filing.py`        | 手工绑新立案单（驳回复制立案后用），保留历史链 | `--case-dir` `--show` / `--new-filing-id <id>` |
| `poll_pending.py`       | 轮询待审核 + 等案号案件，批量回写     | `--root` `[--apply]` `[--status 审核通过]` `[--interval 1]`                        |
| `read_office_text.py`   | 读 `.doc`/`.docx` 正文（起诉状信息源） | `<文件>` `[--out txt]`                                                                |
| `suggest_court.py`      | 诉状没写法院时按管辖规则给候选      | `--case-dir` `[--region]` `[--json]`                                                  |
| `filing_runner.py`      | 一键跑完整链路，默认 dry-run，先过门禁 | `--apply`、`--stage create\|fill\|upload\|submit\|all`、`--rate 1.0`、`--upload-interval 3.5`、`--upload-jitter 1.5`、`--upload-batch 6`、`--upload-cooldown 25`、`--submit --yes`、`--mediation yes\|no`、`--defendant-id-photo upload\|skip`、`--skip-preflight` |
| `confirm_filing.py`     | 打印法院端实际存储内容供确认        | `--case-dir` 或 `--filing-id`、`--json`                                                |
| `query_filing.py`       | 按 id 查状态、抽审核意见、下载退回件、回写本地 | `--case-dir --write` 或 `--filing-id`、`--materials`、`--no-download`、`--json-only` |
| `write_case_note.py`    | 生成 `案件说明.md`（人看的运行结果） | `--case-dir`；查询/提交后自动调用                                                          |
| `resume_upload.py`      | 断点续传：只补没传上的材料（**上传失败一律用它**） | `--case-dir` `[--dry-run]` `[--retries 3]` `[--upload-interval 3.5]`                    |
| `reapply_case.py`       | 撤回→补传→重提（⚠️ 见下方警告）    | `--case-dir` `[--withdraw-only]` `[--no-submit]`                                        |
| `court_client.py`       | 客户端库（登录、建档、材料、提交、预检）  | 供上面脚本 import，也可 `python court_client.py status`                                      |

**两个脚本的红线**

- `resume_upload.py`：上传失败**只用它**。重跑 `filing_runner --stage upload` 会整批重传，
  已成功的那批再传一遍，撞上传频控概率翻倍。
- `reapply_case.py`：逻辑是「状态不是草稿就撤回」。返工完成后案件会回到「待审核」，
  **再跑一次就会把已提交案件重新撤回、重填、重提**，定时任务里跑会变成无限撤提循环。
  跑之前先 `query_filing.py` 确认状态；全部是待审核/已立案就别跑。

凭证走环境变量 `COURT_USERNAME` / `COURT_PASSWORD`，不写进文件。

## 参考文档

| 文件                    | 内容                   | 何时读   |
| --------------------- | -------------------- | ----- |
| `workspace-layout.md` | 目录规范（冻结）、文件归位表、两项闸门  | 开工第一步 |
| `submit-chain.md`     | 一审全链路 API、请求体、码表、材料三步  | 写代码前  |
| `execute-chain.md`    | **强制执行链路**：建单字段差异、执行依据/原审信息接口、执行槽位、诉讼地位码 | 做执行案件前 |
| `login-flow.md`       | SM2、验证码、token、非交互降级、账号类型  | 登录相关  |
| `known-issues.md`     | 实测 Bug 清单（含真实字段名与证据） | 动手前必读 |
| `compliance.md`       | 合规红线、礼貌轮询、脱敏、分发      | 对外交付前 |

## 合规红线

1. 只操作用户已授权的法院账号；凭证加密存储，不写日志、不进分发文件。
2. 验证码走"OCR + 人工确认"双轨，禁止爆破式重试。
3. **最终提交必须经用户明确确认**，禁止静默全自动提交。
4. 请求间隔 ≥ 1 秒，单账号并发 1，失败指数退避，单链路 ≤ 3 次。
5. 日志脱敏：不记密码、token、cookie、身份证号、手机号。
6. 输出附免责声明：结果为辅助整理，最终以法院审核为准。

## 许可

源码开放；个人研究、学习、非商业用途免费使用；**任何商业用途需单独获得作者授权**。协议全文见仓库根目录 [LICENSE](../LICENSE)（PolyForm Noncommercial License 1.0.0）。
