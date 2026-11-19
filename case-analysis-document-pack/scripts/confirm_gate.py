#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""律师确认门禁：分析结果确认后才允许生成 Word。

确认只留痕在 05案件信息/analysis_confirm.json，不生成客户签字文件。
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _sha256(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_type(case_dir: Path, explicit: str) -> str:
    if explicit:
        return explicit
    spec = _load(case_dir / "05案件信息" / "spec.json")
    return str(spec.get("case_type") or "sp")


def confirm(case_dir: Path, adjustments: Dict[str, Any], complaint_format: str, case_type: str) -> Dict[str, Any]:
    state_dir = case_dir / "05案件信息"
    report = case_dir / "案件分析报告.md"
    if not report.exists():
        raise SystemExit(f"缺少案件分析报告：{report}")
    path = state_dir / "analysis_confirm.json"
    current = _load(path) if isinstance(_load(path), dict) else {}

    merged_adjustments = list(current.get("adjustments") or [])
    for key, value in adjustments.items():
        merged_adjustments.append({"item": key, "value": value, "at": datetime.now().isoformat(timespec="seconds")})

    result: Dict[str, Any] = {
        "case_dir": str(case_dir),
        "case_type": case_type,
        "analysis_report": "案件分析报告.md",
        "analysis_hash": _sha256(report),
        "confirmed": True,
        "confirmed_by": "律师",
        "confirmed_at": datetime.now().isoformat(timespec="seconds"),
        "adjustments": merged_adjustments,
        "complaint_format": complaint_format or current.get("complaint_format") or "",
        "complaint_format_asked_at": (
            datetime.now().isoformat(timespec="seconds") if complaint_format else current.get("complaint_format_asked_at", "")
        ),
        "notes": "客户确认表已取消；确认留痕仅用于律师复核。",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def check(case_dir: Path) -> Dict[str, Any]:
    state_dir = case_dir / "05案件信息"
    data = _load(state_dir / "analysis_confirm.json")
    problems: List[str] = []
    if not isinstance(data, dict) or not data:
        problems.append("缺少 analysis_confirm.json，请先律师确认分析结果")
        data = {}
    report = case_dir / "案件分析报告.md"
    if not report.exists():
        problems.append("缺少案件分析报告.md")
    elif str(data.get("analysis_hash") or "") != _sha256(report):
        problems.append("报告 hash 与确认记录不一致：确认后报告被改动，请重新确认")

    case_type = str(data.get("case_type") or "")
    if case_type not in {"sp", "zx"}:
        problems.append("case_type 必须是 sp 或 zx")
    if case_type == "sp" and str(data.get("complaint_format") or "") not in {"ordinary", "elements"}:
        problems.append("一审案件确认后必须选择普通版或要素式诉状（ordinary/elements）")
    if not data.get("confirmed"):
        problems.append("confirmed 不是 true，禁止生成 Word")
    if not (state_dir / "analysis.json").exists():
        problems.append("缺少 analysis.json，请先运行 analyze_case.py")

    return {
        "ok": not problems,
        "problems": problems,
        "case_type": case_type,
        "complaint_format": data.get("complaint_format") or "",
        "hint": "" if not problems else "确认门禁未通过，禁止进入文书渲染阶段。",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="律师确认门禁")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--adjustments-json", default="", help="律师调整 JSON：{项目: 值}")
    ap.add_argument("--complaint-format", choices=["ordinary", "elements"], default="")
    ap.add_argument("--case-type", choices=["sp", "zx"], default="")
    args = ap.parse_args()

    case_dir = Path(args.case_dir).resolve()
    if not case_dir.exists():
        raise SystemExit(f"案件目录不存在：{case_dir}")

    if args.confirm:
        adjustments: Dict[str, Any] = {}
        if args.adjustments_json:
            path = Path(args.adjustments_json).resolve()
            if not path.exists():
                raise SystemExit(f"调整文件不存在：{path}")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                adjustments = loaded
        result = confirm(case_dir, adjustments, args.complaint_format, _case_type(case_dir, args.case_type))
        print(json.dumps({"ok": True, "confirm": result}, ensure_ascii=False, indent=2))
        if not args.check:
            return 0

    result = check(case_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
