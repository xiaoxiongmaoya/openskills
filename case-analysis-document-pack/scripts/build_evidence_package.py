#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把聊天记录/截图/凭证整理成证据包：按证明目的分组 + 生成证据目录。

解决什么：客户丢来一堆截图，人工分不清哪张证明什么。本脚本结合
chat_evidence.json（消息）+ image_labels.json（图片用途）输出：
  04未签稿/证据包/         复制/软链图片，按分组命名
  04未签稿/证据目录.md      编号 + 名称 + 证明目的 + 来源
  05案件信息/evidence_package.json   机器可读清单

分组规则（按图/消息里出现的类型标签）：
  身份证明 / 还款记录 / 劳务或合同依据 / 催收记录 / 委托文件 / 无关或存疑

用法：
  python scripts/build_evidence_package.py --case-dir "<案件目录>" [--json]
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

# 分组规则表：从上到下匹配，命中即止。要加新类型只在这里加一行，不用改逻辑。
# 每条 (分组名, 关键词列表)；关键词命中“类型+用途+证据价值”任一即归入该组。
GROUP_RULES: List[tuple] = [
    ("无关或存疑", ["无关", "不采信", "时间不符", "与本无关", "重复"]),
    ("身份证明", ["身份", "身份证", "户口", "营业执照", "工商", "法定代表人"]),
    ("委托文件", ["委托", "律师", "告知", "授权", "代理"]),
    ("合同或协议", ["合同", "协议", "欠条", "借条", "收据", "订单", "对账"]),
    ("款项往来", ["还款", "转账", "账单", "支付", "汇款", "流水", "收付款", "银行"]),
    ("沟通记录", ["催", "聊天", "录音", "短信", "通话", "微信记录", "邮件"]),
    ("侵权或损害", ["伤情", "诊断", "病历", "事故认定", "鉴定", "现场", "照片", "损失"]),
    ("程序文件", ["起诉", "判决", "裁定", "调解书", "仲裁", "送达", "立案"]),
]
DEFAULT_GROUP = "其他"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _group_of(label: Dict[str, Any]) -> str:
    """按规则表归类；无关判断排第一，避免“转账详情（与本案无关）”被误归款项往来。"""
    text = (str(label.get("类型") or "") + str(label.get("用途") or "")
            + str(label.get("证据价值") or ""))
    for group, keywords in GROUP_RULES:
        if any(k in text for k in keywords):
            return group
    return DEFAULT_GROUP


def build(case_dir: Path) -> Dict[str, Any]:
    case_dir = case_dir.resolve()
    labels = _load(case_dir / "05案件信息" / "image_labels.json")
    chat = _load(case_dir / "05案件信息" / "chat_evidence.json")
    if not isinstance(labels, dict):
        labels = {}
    if not isinstance(chat, dict):
        chat = {}

    evidence_dir = case_dir / "04未签稿" / "证据包"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for old in evidence_dir.iterdir():
        if old.is_file():
            old.unlink()

    entries: List[Dict[str, Any]] = []
    seq = 1

    # 1) 图片证据（有用途标注的才进包，无关的单独归组）
    for rel, label in labels.items():
        if not isinstance(label, dict):
            continue
        src = case_dir / rel
        if not src.exists():
            continue
        group = _group_of(label)
        usage = str(label.get("用途") or Path(rel).stem)
        tag = "存疑" if group == "无关或存疑" else f"{seq}"
        target_name = f"{tag}-{src.name}" if group != "无关或存疑" else f"存疑-{src.name}"
        target = evidence_dir / target_name
        try:
            shutil.copyfile(src, target)
        except Exception:
            target = src
        entries.append({
            "编号": seq if group != "无关或存疑" else None,
            "分组": group,
            "名称": Path(rel).name,
            "证明目的": usage,
            "原路径": rel,
            "包内路径": f"04未签稿/证据包/{target.name}" if target != src else rel,
            "存疑": group == "无关或存疑",
        })
        if group != "无关或存疑":
            seq += 1

    # 2) 聊天记录导出 / 凭证 PDF 等非图片材料
    for src in chat.get("sources") or []:
        if not isinstance(src, dict):
            continue
        if src.get("type") == "text" or str(src.get("file", "")).lower().endswith(".pdf"):
            rel = src.get("file") or ""
            p = case_dir / rel
            if p.exists() and p.suffix.lower() == ".pdf":
                target = evidence_dir / p.name
                try:
                    shutil.copyfile(p, target)
                except Exception:
                    target = p
                entries.append({
                    "编号": seq, "分组": "其他", "名称": p.name,
                    "证明目的": "电子转账凭证/书证",
                    "原路径": rel,
                    "包内路径": f"04未签稿/证据包/{target.name}" if target != p else rel,
                    "存疑": False,
                })
                seq += 1

    # 证据目录
    lines = ["# 证据目录", "", f"生成时间：{datetime.now().isoformat(timespec='seconds')}", ""]
    lines.append("| 编号 | 证据名称 | 证明目的 | 分组 | 包内路径 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for e in entries:
        num = e["编号"] if e["编号"] is not None else "存疑"
        lines.append(f"| {num} | {e['名称']} | {e['证明目的']} | {e['分组']} | `{e['包内路径']}` |")
    lines.append("")
    doubtful = [e for e in entries if e["存疑"]]
    if doubtful:
        lines += ["## 存疑/不采信材料（不进正式证据目录）", ""]
        for e in doubtful:
            lines.append(f"- {e['名称']}：{e['证明目的']}")
        lines.append("")

    outline = evidence_dir / "证据目录.md"
    outline.write_text("\n".join(lines), encoding="utf-8")

    manifest = {
        "case_dir": str(case_dir),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "evidence_dir": str(evidence_dir),
        "total": len([e for e in entries if not e["存疑"]]),
        "doubtful": len(doubtful),
        "entries": entries,
    }
    (case_dir / "05案件信息" / "evidence_package.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"ok": True, "evidence_dir": str(evidence_dir),
            "catalog": f"04未签稿/证据包/{outline.name}",
            "total": manifest["total"], "doubtful": manifest["doubtful"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="整理证据包并生成证据目录")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    result = build(Path(args.case_dir))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
