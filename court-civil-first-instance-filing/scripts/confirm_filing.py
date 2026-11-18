#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打印「法院实际存了什么」的确认单——只用于人工确认，数据全部回读自法院。

本地 spec 只是意图；本脚本展示的是远端真值，写错的地方直接标 [!!]。

    python confirm_filing.py --case-dir "<案件目录>"
    python confirm_filing.py --filing-id 0123456789abcdef... --session-file ".../.court_session.json"
    python confirm_filing.py --case-dir "<案件目录>" --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from court_client import CourtClient, session_file_for  # noqa: E402

TICK = "[OK]"
FLAG = "[!!]"


def mark(ok: bool) -> str:
    return TICK if ok else FLAG


def render(snap: Dict[str, Any]) -> str:
    lines = []
    status = snap.get("status") or {}
    lines.append("=" * 58)
    lines.append("以下为法院端实际存储内容（非本地准备值）")
    lines.append(f"回读时间：{snap.get('checked_at')}    来源：{snap.get('_source')}")
    lines.append("=" * 58)
    lines.append(f"立案单：{snap.get('id')}")
    origin = snap.get("origin") or {}
    lines.append(f"来源　：{origin.get('label') or '-'}"
                 + (f"（复制自 {origin.get('copied_from')}）" if origin.get("copied_from") else ""))
    lines.append(f"状态　：{status.get('status_text')}（{status.get('status')}）"
                 f"  提交时间 {status.get('submitted_at') or '-'}")
    lines.append(f"法院　：{snap.get('court')}（fyid={snap.get('fyid')}）")

    cause = snap.get("cause") or {}
    is_zx = snap.get("case_type") == "zx"
    if is_zx:
        lines.append(f"执行依据：{cause.get('zxyjAh')!r} {mark(bool(cause.get('display_ok')))}")
    else:
        lines.append(f"案由　：laay={cause.get('laay')!r} {mark(bool(cause.get('display_ok')))}"
                     f"    节点id={cause.get('laayMz')!r} {mark(bool(cause.get('id_ok')))}")
        if not cause.get("display_ok"):
            extra = cause.get("fixed") or {}
            lines.append(f"        {FLAG} 案由显示异常：应为名称，节点 id 应为数字。"
                         f"对照正确值：laay={extra.get('name')!r} laayMz={extra.get('id')!r}")
        lines.append(f"标的额：{snap.get('amount')}")

    ajmc = str(snap.get("ajmc") or "").strip()
    lines.append(f"案件名：{ajmc or '（空）'} {mark(bool(ajmc))}"
                 + ("" if ajmc else f"  {FLAG} 案名为空会被服务端拒（RMFY109905），先跑 --stage fill 重算"))
    mc = snap.get("dsrMc") or {}
    lines.append(f"参与人：{mc.get('value')!r} {mark(bool(mc.get('ok')))}")

    lines.append("-" * 58)
    for party in snap.get("parties") or []:
        lines.append(f"{party.get('role') or '?'}　{party.get('name') or ''}"
                     f"　证件 {party.get('id_card') or '（未填）'}")
    for agent in snap.get("agents") or []:
        lines.append(f"代理人　{agent.get('name')}　执业证 {agent.get('license')}"
                     f"　{agent.get('firm')}　被代理人 {agent.get('for_party')}")

    lines.append("-" * 58)
    lines.append("材料（槽位 → 已传文件）")
    for item in snap.get("materials") or []:
        files = "、".join(item.get("files") or []) or "（空）"
        tag = "" if item.get("required") else "（非必传）"
        lines.append(f"  {mark(bool(item.get('ok')))} {item.get('slot')}{tag}：{files}")

    gates = snap.get("gates") or {}
    lines.append("-" * 58)
    lines.append("提交前答疑")
    med = gates.get("mediation")
    if med is not None:
        if is_zx:
            lines.append("  - 先行调解：不适用（执行案件）")
        else:
            lines.append(f"  {mark(bool(med.get('ok')))} 先行调解：{med.get('value')}（{med.get('code') or '空'}）")
            if med.get("note") and not med.get("ok"):
                lines.append(f"        {FLAG} {med['note']}")
    photo = gates.get("defendant_id_photo") or {}
    if is_zx:
        lines.append(f"  {mark(bool(photo.get('ok')))} 被告身份证照片：不适用（执行案件）")
    else:
        lines.append(f"  {mark(bool(photo.get('ok')))} 被告身份证照片：{photo.get('value')}"
                     f"（槽内{'有' if photo.get('uploaded') else '无'}被告证件文件）")

    claims = snap.get("claims") or {}
    lines.append(("执行请求" if is_zx else "诉请")
                 + f" {claims.get('ssqq_len')} 字 / "
                 + ("执行理由" if is_zx else "事实理由")
                 + f" {claims.get('ssly_len')} 字")

    problems = snap.get("problems") or []
    lines.append("=" * 58)
    if problems:
        lines.append(f"预检未通过（{len(problems)} 项）：")
        for p in problems:
            lines.append(f"  {FLAG} {p}")
    else:
        lines.append("预检通过：可以提交")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="打印法院端实际存储内容，供人工确认")
    ap.add_argument("--case-dir", default="")
    ap.add_argument("--filing-id", default="")
    ap.add_argument("--session-file", default="")
    ap.add_argument("--mediation", choices=["yes", "no"], default="")
    ap.add_argument("--defendant-id-photo", choices=["upload", "skip"], default="")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--case-type", choices=["sp", "zx"], default="sp",
                    help="sp=民事一审（默认）/ zx=强制执行申请")
    ap.add_argument("--user-type", default="lsyh", help="登录账号类型：lsyh/gryh/fryh")
    args = ap.parse_args()

    case_dir: Optional[Path] = Path(args.case_dir).resolve() if args.case_dir else None
    session = Path(args.session_file) if args.session_file else (
        session_file_for(case_dir) if case_dir else Path(".court_session.json"))
    filing_id = args.filing_id
    if not filing_id and case_dir:
        for name in ("case.json", "draft.json"):
            f = case_dir / "05案件信息" / name
            if f.exists():
                filing_id = json.loads(f.read_text(encoding="utf-8")).get("filing_id") or ""
                if filing_id:
                    break
    if not filing_id:
        raise SystemExit("需要 --filing-id，或 --case-dir 下 case.json 里已有 filing_id")

    decisions = {"mediation": args.mediation, "defendant_id_photo": args.defendant_id_photo}
    client = CourtClient(base_url="https://zxfw.court.gov.cn", user_type=args.user_type,
                         session_file=str(session), polite_delay=0.8)
    snap = client.confirm_snapshot(filing_id, decisions, case_type=args.case_type)
    print(json.dumps(snap, ensure_ascii=False, indent=2) if args.json else render(snap))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
