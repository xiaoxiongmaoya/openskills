# 登录链路参考（法院诉讼服务平台）

适用接口族：`{base}/yzw/yzw-zxfw-yhfw/api/v1/*`。默认 `base_url = https://zxfw.court.gov.cn`，`api_base = {base_url}/yzw/yzw-zxfw-yhfw`。各地同构法院诉讼服务网仅需替换 `base_url`。

## 1. 接口顺序

| 步骤 | 方法 | 路径 | 关键字段 | 说明 |
| --- | --- | --- | --- | --- |
| 1 取公钥 | GET | `/api/v1/publicKey` | `data`：SM2 公钥 hex 串 | 可能带 `04` 前缀；需补齐为 128 位 hex |
| 2 取验证码 | GET | `/api/v1/captcha` | `data.id`、`data.jpg`/`data.img` | 图片为 base64；`id` 与图片必须配对使用 |
| 3 识别验证码 | 本地 | — | 期望长度 4 | ddddocr + 4 变体投票；失败转人工 |
| 4 登录 | POST | `/api/v1/login` | `login`/`pass`/`yzm`/`id`/`type` | 密码 SM2 加密后 hex |

## 2. 登录请求体

```json
{
  "login": "<法院账号>",
  "pass": "<SM2(明文密码) 的 hex>",
  "yzm": "<验证码 4 位>",
  "id": "<第 2 步 captcha.id>",
  "type": "lsyh"
}
```

`type` 为用户类型：`lsyh` = 律师用户；其他取值以登录页实际下拉为准，不要臆造。

## 3. SM2 加密（gmssl）

```python
from gmssl import sm2

def sm2_encrypt(public_key_hex: str, plaintext: str) -> str:
    key = (public_key_hex or "").strip()
    if key.startswith("04"):
        key = key[2:]
    if len(key) < 128:                     # 仅给 x 或分量不足时补齐
        half = len(key) // 2
        key = key[:half].zfill(64) + key[half:].zfill(64)
    cryptor = sm2.CryptSM2(public_key=key, private_key="", asn1=False, mode=1)
    return cryptor.encrypt(plaintext.encode("utf-8")).hex()
```

要点：

- 公钥每次登录前重新获取，不要长期缓存复用。
- `mode=1`（C1C2C3）、`asn1=False`，与法院前端 SM2 组件保持一致；换平台先用前端 bundle 核对。
- 加密结果 hex 大写小写均可，服务端按 hex 解析。

## 4. 验证码 OCR 策略

- 引擎：ddddocr（Python 生态，法院验证码 4 位字母数字混合）。
- 提升通过率：对原图跑 4 种预处理变体（`base` / `enhanced` / `sharpen` / `invert`）分别识别，投票取多数；票数相同取置信度高者。
- 字符纠错（法院验证码不含易混字符时的双向纠正表）：

| 规则 A（数字优先） | 规则 B（字母优先） |
| --- | --- |
| 0→O, 1→I, 2→Z, 5→S, 6→G, 8→B | O→0, I→1, L→1, Z→2, S→5, G→6, B→8 |

- 兜底：任一变体识别结果长度 ≠ 4、或投票不一致、或置信度低 → 转人工输入，**不允许无限重试**。
- 单次登录验证码尝试 ≤ 3 次；每次尝试必须重新取验证码（`id` 一次性）。

## 5. 成功判定与 token 管理

成功判定（满足其一）：

- `payload["code"] == 200` 或 `payload["success"] is True`
- `payload["data"]` 内含 `token` / `userId` / `loginName`

token 提取顺序：

1. 响应头 `token` → `Token` → `Authorization`
2. body `data.token` / `data.Authorization` / `data.authorization`
3. body 顶层 `token` / `Authorization` / `authorization`

token 管理：

- 写入 session 请求头 `Authorization`（大小写两种都设置，兼容服务端）。
- JWT  token 解第二段 payload 的 `exp`；过期前 3 分钟视为过期，主动重登。
- 会话持久化：cookies（`requests.utils.dict_from_cookiejar`）+ token + 过期时间写入本地 JSON（权限 600）；复用前先验过期，过期或 401 立即重登。

## 6. 错误分类与处理

| 返回特征 | 判定 | 处理 |
| --- | --- | --- |
| message 含"密码" | 密码错误 | 标记 `password_invalid`，停止自动重试，提示用户改密 |
| message 含"验证码" | 验证码错误 | 重取验证码重试，≤ 3 次 |
| 5xx / 超时 | 平台异常 | 指数退避（2s/4s/8s），≤ 3 次，仍失败交人工 |
| 401 / token 失效 | 会话过期 | 清持久化会话，重新走登录四步 |

## 7. 请求头基线

```
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36
Accept: application/json, text/plain, */*
Accept-Language: zh-CN,zh;q=0.9
Origin: {base_url}
Referer: {base_url}/zxfw/index.html#/pagesGrxx/pc/login/index
Content-Type: application/json;charset=UTF-8   (POST/PATCH JSON 体)
```

Chrome 大版本号随时间推移会过期，以用户本机 UA 为准定期更新，避免固定值被识别。

## 8. 非交互环境（脚本 / Agent）登录降级方案

实测环境：`ddddocr` 在本机 onnxruntime 上 **import 即段错误**（exit 139，进程直接挂，`try/except` 拦不住）；且 `input()` 兜底在无 stdin 环境会卡死。

可用流程（三步入）：

```python
from court_client import CourtClient

c = CourtClient(base_url="https://zxfw.court.gov.cn", username=u, password=p, interactive=False)
cap = c.dump_captcha("captcha.png")          # 1 取验证码并落盘，返回 {"id","image"}
# 2 用视觉模型/人工看图得到 4 位码
c.login_with_captcha(cap["id"], "jeut")      # 3 用同一 captcha_id 登录
```

要点：

- `login_with_captcha` 不会重新取验证码，避免 `id` 与图片错配；旧版 `login(captcha_code=...)` 会重新 `fetch_captcha()` 导致必然失败，已修。
- `interactive=False` 时 OCR 失败会落盘图片并抛错（附带路径），不会阻塞。
- 验证码 `id` 一次性，失败必须重新 `dump_captcha`。
- 登录成功判据与 token 处理同第 5 节；返回 `{"username":"张三","role":"2","roleText":"律师","dsrlx":"1501_000011-1"}` 说明是律师用户（`type=lsyh`）。
