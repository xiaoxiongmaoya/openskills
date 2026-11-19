# 文书与法院材料槽位映射

**唯一来源**：老 Skill 的常量，本技能不得重新定义编码。

| 来源文件 | 常量 | 行号 |
| --- | --- | --- |
| `d:\Project\skills\court-civil-first-instance-filing\scripts\filing_runner.py` | `SLOT_ALIAS` | L43-56 |
| 同上 | `EXTRA_COPY = {"账户确认书": "其他材料"}` | L57 |
| `d:\Project\skills\court-civil-first-instance-filing\scripts\court_client.py` | `EXEC_SLOTS` | L90-97 |

`doc_slots.py` 通过 AST 只读解析上述常量（不 import、不运行老 Skill 的 main、不触发任何网络登录），对外提供：

```python
slot_code_for(doc_name: str, case_type: str) -> str
extra_copy_for(doc_name: str) -> str | None
```

## 映射表（由常量派生，不手写）

| 文书 | 槽位名 | 编码 |
| --- | --- | --- |
| 起诉状 / 执行申请书 | 起诉状 / 执行申请书 | 11800016-2 |
| 证据目录 | 证据 | 11800016-4 |
| 送达地址确认书 | 送达地址确认书 | 11800016-254 |
| 账户确认书 / 收款账户确认书 | 账户确认书 / 收款账户确认书 | 11800016-36 |
| 其他材料（账户确认书双投） | 其他材料 | 11800016-3 |
| 授权委托书、律所函、律师证 | 代理人手续 / 授权委托书及代理人身份证明 | 11800016-9 |
| 申请人身份证明（客户提供） | 当事人身份证明 / 申请人身份材料 | 11800016-1 |
| 执行依据文书（客户提供） | 执行依据文书 | 11800016-8 |

## 校验

`render_documents.py` 写出的 `document_manifest.json` 中每项 `slot_code` 必须与 `doc_slots.slot_code_for()` 相等；`preflight_materials.py` 按 `--case-type sp|zx` 做离线门禁。

账户确认书需双投：`EXTRA_COPY` 由老 Skill 常量给出，本技能只在 `提交材料清单.md` 里提示，不执行上传。
