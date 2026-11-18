#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 `案件说明.md`（整篇重写，人看的运行结果都在这）。

    python write_case_note.py --case-dir "<案件目录>"

数据来源：05案件信息/{case.json, spec.json, 立案单状态.json}。
查询状态、提交完成后会自动调用，不需要手写。

json 只是机器状态；给律师看的是这份 md。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict

STATUS_HINT = {
    "草稿": "草稿，尚未提交",
    "待审核": "已提交，等待法院审核",
    "审核通过": "审核通过，等待立案/缴费",
    "审核未通过": "审核未通过，需看审核意见补正",
    "已立案": "已立案，可查案号",
    "补充材料": "需补充材料",
}
MED_TXT = {"yes": "同意调解", "no": "不同意调解"}
PHOTO_TXT = {"upload": "上传", "skip": "不上传"}
SUBMITTED = ("待审核", "审核通过", "审核未通过", "已立案", "补充材料")


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _party_names(spec: Dict[str, Any], role: str, case: Dict[str, Any] | None = None) -> str:
    """多当事人姓名拼接：优先 plaintiffs/defendants 列表，回退单数，再退回 case.json。"""
    plural = spec.get("plaintiffs") if role == "plaintiff" else spec.get("defendants")
    if isinstance(plural, list) and plural:
        names = [str(p.get("name") or "") for p in plural if isinstance(p, dict) and p.get("name")]
        if names:
            return "、".join(names)
    single = spec.get("plaintiff" if role == "plaintiff" else "defendant") or {}
    if isinstance(single, dict) and single.get("name"):
        return str(single["name"])
    if case:
        return str(case.get(role) or "")
    return ""


def _progress(case_dir: Path) -> list[tuple[bool, str]]:
    state = case_dir / "05案件信息"
    case, spec, snap = _load(state / "case.json"), _load(state / "spec.json"), _load(state / "立案单状态.json")
    signed = [p for p in (case_dir / "02签字件").glob("*") if p.is_file() and not p.name.startswith(".")]
    uploaded = sum(len(v) for v in (snap.get("materials") or {}).values())
    decisions = case.get("decisions") or {}
    med = (decisions.get("mediation") or {}).get("value")
    photo = (decisions.get("defendant_id_photo") or {}).get("value")
    info_ok = bool(_party_names(spec, "plaintiff", case) and _party_names(spec, "defendant", case)
                   and (spec.get("case") or {}).get("amount") and (case.get("filing_id") or ""))
    return [
        (bool(signed), f"材料整理与签字核对（{len(signed)} 份）"),
        (info_ok, "当事人 / 代理人 / 案由 / 标的额"),
        (uploaded > 0, f"材料上传（{uploaded} 份）"),
        (uploaded > 0, "诉请与事实理由"),
        (bool(med), f"是否同意先行调解：{MED_TXT.get(med, '未定')}"),
        (bool(photo), f"是否上传被告身份证照片：{PHOTO_TXT.get(photo, '未定')}"),
        (case.get("status") in SUBMITTED, "预检通过并提交"),
    ]


def render(case_dir: Path) -> str:
    state = case_dir / "05案件信息"
    case, spec, snap = _load(state / "case.json"), _load(state / "spec.json"), _load(state / "立案单状态.json")
    court = case.get("court") or {}
    status = case.get("status") or snap.get("status_text") or "未提交"
    plaintiff = _party_names(spec, "plaintiff", case)
    defendant = _party_names(spec, "defendant", case)
    cause = (spec.get("case") or {}).get("cause") or case.get("cause") or ""
    amount = str((spec.get("case") or {}).get("amount") or "")

    out = [f"# {plaintiff} 诉 {defendant} {cause}".strip(), ""]
    out.append(f"- 受理法院：{court.get('name', '')}（fyid={court.get('fyid', '')}）")
    out.append(f"- 案由：{cause}")
    out.append(f"- 案件目录：`{case_dir}`")
    out.append(f"- 状态：{status}" + (f"（{STATUS_HINT[status]}）" if status in STATUS_HINT else ""))
    out += ["", "## 目录", "",
            "| 目录 | 用途 |", "| --- | --- |",
            "| `01原始包/` | 客户原始压缩包 + `解压/`，只读 |",
            "| `02签字件/` | 已签字盖章的 PDF，直接上传 |",
            "| `03图片件/` | 身份证、借条、截图 |",
            "| `04未签稿/` | Word 原稿（**文书信息只从这里读**） |",
            "| `05案件信息/` | spec / 草稿 id / 提交结果 / 决策记录 |",
            "| `06运行记录/` | 会话、验证码、日志（勿外发） |",
            "", "## 进度", ""]
    out += [f"- [{'x' if ok else ' '}] {text}" for ok, text in _progress(case_dir)]

    out += ["", "---", "", "## 运行结果", ""]
    out.append(f"- 状态：**{status}**")
    out.append(f"- 立案单号：`{case.get('filing_id') or snap.get('id') or '—'}`")
    submitted_at = case.get("submitted_at") or snap.get("submitted_at")
    if submitted_at:
        out.append(f"- 提交时间：{submitted_at}")
    if snap.get("case_no"):
        out.append(f"- 法院案号：{snap['case_no']}")

    queue = case.get("court_queue")
    court_line = f"- 受理法院：{court.get('name', '')}（fyid={court.get('fyid', '')}）"
    if queue:
        court_line += f"，当前排期在前的案件 **{queue} 件**"
    out.append(court_line)
    if queue and case.get("court_queue_at"):
        out.append(f"- 排期数取样时间：{case['court_queue_at']}")
    out.append(f"- 标的额：{int(amount):,} 元" if amount.isdigit() else f"- 标的额：{amount or '—'}")

    decisions = case.get("decisions") or {}
    med = (decisions.get("mediation") or {}).get("value")
    photo = (decisions.get("defendant_id_photo") or {}).get("value")
    if med or photo:
        out.append(f"- 用户决策：{MED_TXT.get(med, '未记录')}；被告身份证照片{PHOTO_TXT.get(photo, '未记录')}")
    if snap.get("materials"):
        out.append(f"- 已传材料：{sum(len(v) for v in snap['materials'].values())} 份"
                   f"（{len(snap['materials'])} 个槽位）")
    checked = case.get("last_checked_at") or snap.get("checked_at")
    if checked:
        out.append(f"- 最近回读：{checked}")
    if case.get("note"):
        out.append(f"- 备注：{case['note']}")

    records = snap.get("review_records") or case.get("review_records") or []
    out += ["", "## 法院审核结果", ""]
    if not records:
        out.append("尚未出结果，或法院端未填写审核意见。")
    else:
        for rec in records:
            out.append(f"### {rec.get('reviewed_at') or '时间未记录'} · {rec.get('result') or '—'}")
            out.append("")
            if rec.get("reviewer"):
                out.append(f"- 审核人：{rec['reviewer']}")
            if rec.get("reject_reason"):
                out.append(f"- 驳回原因：{rec['reject_reason']}")
            if rec.get("material_fix_at"):
                out.append(f"- 材料补正时间：{rec['material_fix_at']}")
            opinion = str(rec.get("opinion") or "").strip()
            rows = [line.strip() for line in opinion.splitlines() if line.strip()]
            if rows:
                out.append("- 审核意见（法院原文，需按此补正）：")
                out.append("")
                for line in rows:
                    out.append(f"  > {line}")
                out.append("")
            local = [f for f in (snap.get("returned_files") or []) if f.get("name")]
            if rec.get("returned_files") and local:
                out.append("- 法院退回附件（已下载，可直接改）：")
                for item in local:
                    out.append(f"  - `{item.get('path') or item['name']}`")
            out.append("")

    # 立案单绑定链：驳回后重新提交会换 filing_id，历史单不能丢
    chain = case.get("filings") or []
    if len(chain) > 1:
        current_id = str(case.get("filing_id") or "")
        out += ["", "## 立案单变更链", "",
                "| # | 立案单 | 状态 | 提交时间 | 审核结论 | 备注 |",
                "| --- | --- | --- | --- | --- | --- |"]
        for idx, item in enumerate(chain, 1):
            mark = "（当前）" if str(item.get("filing_id")) == current_id else ""
            note = item.get("note") or ""
            if item.get("superseded_by"):
                note = f"被 {item['superseded_by']} 取代 {item.get('superseded_at') or ''}".strip()
            out.append(f"| {idx} | `{item.get('filing_id')}`{mark} | {item.get('status') or '—'} | "
                       f"{item.get('submitted_at') or '—'} | {item.get('review_at') or '—'} | {note} |")
        out.append("")
        out.append("换单不代表旧单消失：旧单的驳回意见仍留着，用 "
                   "`bind_filing.py --case-dir <目录> --show` 查看完整链路。")

    out += ["", "刷新本文（会重新拉取法院状态）：", "",
            "```bash",
            f'python scripts/query_filing.py --case-dir "{case_dir}" --write',
            "```", ""]
    return "\n".join(out)


def note_filename(case_dir: Path) -> str:
    """说明文档以客户（原告）名字命名，便于多案归档时区分。"""
    state = case_dir / "05案件信息"
    spec = _load(state / "spec.json")
    case = _load(state / "case.json")
    client = _party_names(spec, "plaintiff", case)
    return f"{client}-案件说明.md" if client else "案件说明.md"


def update(case_dir: Path) -> Path:
    note = case_dir / note_filename(case_dir)
    note.write_text(render(case_dir), encoding="utf-8")
    return note


def main() -> int:
    ap = argparse.ArgumentParser(description="生成案件说明.md（人看的运行结果）")
    ap.add_argument("--case-dir", required=True)
    args = ap.parse_args()
    print(update(Path(args.case_dir).resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
