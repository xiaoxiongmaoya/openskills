# WorkBuddy Skills

自己的 WorkBuddy 技能集合。一个技能一个目录，目录名即技能名。

## 许可协议

本项目源码公开（开源），采用 [PolyForm Noncommercial License 1.0.0](./LICENSE)：个人研究、学习、实验、非商业用途免费使用；**任何商业用途需单独获得作者授权**。详见 [LICENSE](./LICENSE) 全文。两个技能的 `SKILL.md` 末尾同样标注此协议，保持一致。

## 技能列表

| 技能 | 说明 |
| --- | --- |
| [`court-civil-first-instance-filing`](./court-civil-first-instance-filing/) | 法院民事一审在线立案：登录、建草稿、材料上传、诉请、提交、状态查询 |
| [`case-analysis-document-pack`](./case-analysis-document-pack/) | 案件分析与立案材料包：聊天记录解析、法律关系分析、确认后按本地实战模板生成整套可打印签字文书 |

## 结构约定

```text
<skill-name>/
  SKILL.md              # 技能定义（必须）
  connector-meta.json   # 技能元数据
  icon.svg              # 图标
  references/           # 参考文档
  scripts/              # 可执行脚本
```

## 安全约定

- 凭证一律走环境变量，不写进代码或文档
- 会话、案件数据、个人敏感信息不进本仓库
