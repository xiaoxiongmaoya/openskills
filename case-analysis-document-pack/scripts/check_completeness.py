#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""案件完整性检查：找出分析/立案前还缺什么，不替律师猜。"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _text(value: Any) -> str:
    return str(value or "").strip()


PLACEHOLDER_HINTS = ("待补", "待确认", "未提供", "材料未见", "不详", "待核")


def _filled(value: Any) -> str:
    """把“待补/待确认/材料未见”等占位文本视为未填写。"""
    text = _text(value)
    if not text or any(hint in text for hint in PLACEHOLDER_HINTS):
        return ""
    return text


def check(case_dir: Path, analysis: Dict[str, Any] | None = None,
          chat: Dict[str, Any] | None = None, mcp_status: Dict[str, Any] | None = None) -> Dict[str, Any]:
    case_dir = case_dir.resolve()
    state = case_dir / "05案件信息"
    analysis = analysis if isinstance(analysis, dict) else _load(state / "analysis.json")
    chat = chat if isinstance(chat, dict) else _load(state / "chat_evidence.json")
    mcp_status = mcp_status if isinstance(mcp_status, dict) else _load(state / "mcp_status.json")

    findings = analysis.get("agent_findings") or {}
    summary = findings.get("case_summary") or {}
    legal = findings.get("legal_relations") or {}
    facts = analysis.get("facts") or []
    disputes = findings.get("dispute_focus") or []
    grid = findings.get("evidence_grid") or []
    laws = (analysis.get("law_search") or {}).get("results") or []
    cases = (analysis.get("case_search") or {}).get("results") or []
    case_type = _text(analysis.get("case_type") or summary.get("case_type") or "sp")
    connected = bool(mcp_status.get("connected"))

    missing: List[Dict[str, str]] = []
    passed = 0
    total = 0

    def item(field: str, ok: bool, severity: str, hint: str) -> None:
        nonlocal passed, total
        total += 1
        if ok:
            passed += 1
        else:
            missing.append({"field": field, "severity": severity, "hint": hint})

    used_providers: set[str] = set()
    for bucket_key in ("law_search", "case_search"):
        bucket = analysis.get(bucket_key) or {}
        used_providers |= {str(k) for k in (bucket.get("providers") or {}).keys()}

    item("第三方法律数据源", connected or bool(used_providers), "critical",
         "至少连接一个第三方法律数据源（华宇元典 / 企查查法律数据）；本技能无本地知识库可兜底")
    item("双源交叉核验", len(used_providers) >= 2, "warning",
         "建议同时连上元典与企查查，法条与类案双源交叉核对后再定稿")
    item("案件类型", case_type in {"sp", "zx"}, "critical", "明确是一审还是强制执行")
    item("案由", bool(_filled(summary.get("cause"))), "critical", "确定案由；不确定时列候选并让律师确认")
    item("原告/申请执行人", bool(_filled(summary.get("plaintiff"))), "critical", "补齐姓名/名称")
    item("被告/被执行人", bool(_filled(summary.get("defendant"))), "critical", "补齐姓名/名称")
    item("诉讼请求/执行请求", bool(_filled(summary.get("claims"))), "critical", "写清请求事项与金额")
    if case_type == "sp":
        item("标的额", bool(_filled(summary.get("amount"))), "critical", "补齐诉讼标的额")
    else:
        item("执行依据", bool(_filled(summary.get("enforcement_basis"))), "critical", "补生效判决/调解书及生效证明")
        item("执行标的", bool(_filled(summary.get("amount"))), "critical", "补齐执行标的金额/行为")
    item("受理法院", bool(_filled(summary.get("court"))), "warning",
         "管辖是法律判断，需律师确认后由立案阶段匹配 fyid")
    item("原告身份信息", bool(_filled(summary.get("plaintiff_id"))), "warning", "补证件号/统一社会信用代码")
    item("原告地址/电话", bool(_filled(summary.get("plaintiff_address")) and _filled(summary.get("plaintiff_phone"))),
         "warning", "送达地址确认书和立案信息需要")
    item("被告身份信息", bool(_filled(summary.get("defendant_id"))), "critical",
         "补被告身份证号/统一社会信用代码；仅有姓名和手机号无法立案")

    sourced = [f for f in facts if isinstance(f, dict)
               and isinstance(f.get("source"), dict)
               and (_text(f["source"].get("file")) or _text(f["source"].get("locator")))]
    item("有来源的事实", bool(sourced), "critical", "没有来源的事实不能写入报告")
    item("事实时间线", bool(analysis.get("timeline")), "critical", "至少形成一条可回查时间线")

    step_keys = ("step1", "step2", "step3", "step4", "step5")
    item("法律关系五步骤", all(_text(legal.get(k)) for k in step_keys), "critical",
         "按两阶段五步骤补齐：争议点/是否成立/性质/要素/变动")
    item("三段论涵摄", bool(_text(legal.get("syllogism"))), "critical",
         "补小前提—大前提—结论，并提示不确定性")
    item("争议焦点", bool(disputes), "critical", "至少归纳一个争议焦点")
    item("证据三栏", bool(grid), "warning", "补主张—事实—证据—弱点")
    item("法条检索", bool(laws), "warning",
         "未检索到法条时标待核验；连接第三方后补 source_id")
    item("类案检索", bool(cases), "warning",
         "未检索到类案时标待核验；连接第三方后补 1–5 个案号")
    item("风险/待核验", bool(findings.get("risks") or findings.get("questions")), "warning",
         "补诉讼时效、管辖、主体资格、证据缺口等风险")

    score = round(passed / total * 100) if total else 0
    result: Dict[str, Any] = {
        "case_dir": str(case_dir),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "case_type": case_type,
        "score": score,
        "passed": passed,
        "total": total,
        "missing": missing,
        "critical_count": sum(1 for x in missing if x["severity"] == "critical"),
        "warning_count": sum(1 for x in missing if x["severity"] == "warning"),
        "ok": not any(x["severity"] == "critical" for x in missing),
        "note": "critical 未补齐时不应生成正式报告/文书；warning 可由律师判断后带风险推进。",
    }
    state.mkdir(parents=True, exist_ok=True)
    (state / "completeness.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="案件完整性检查")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    case_dir = Path(args.case_dir).resolve()
    if not case_dir.exists():
        raise SystemExit(f"案件目录不存在：{case_dir}")
    result = check(case_dir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"完整度：{result['score']}%（{result['passed']}/{result['total']}）")
        for item in result["missing"]:
            print(f"  [{item['severity']}] {item['field']}：{item['hint']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
