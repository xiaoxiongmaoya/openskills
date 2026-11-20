#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键类案/法条检索：自动从案件分析结果生成查询词，产出 4 份 MCP 调用参数，
执行完再把原始返回批量 ingest 并回写报告。

Agent 侧仍需执行 MCP（脚本不联网），但不再手工想查询词、不再逐条拼命令：
  1) plan       -> 05案件信息/mcp_calls/{provider}_{kind}.json + search_plan.json
  2) 执行 4 次 MCP，原始返回落到 06运行记录/mcp_raw/{provider}_{kind}.json
  3) ingest-all -> 自动识别文件、批量 ingest、可选 --refresh 重跑分析报告
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import yuandian_search as ys  # noqa: E402

STATE_DIR = "05案件信息"
RUN_DIR = "06运行记录"
RAW_SUBDIR = "mcp_raw"
CALL_SUBDIR = "mcp_calls"

# 争点里的疑问成分，生成检索词时整句替换掉，避免把“是否”“能否”当关键词
SHELL_WORDS = (
    "是否存在", "是否有效", "是否成立", "是否能", "是否为", "尚欠金额是否为",
    "如何认定", "如何确定", "怎么认定", "能否成立", "成立与否",
    "是否", "能否", "可否", "应否", "有无",
)
# 剥离疑问成分后仍留在句首的谓词，一并去掉
LEAD_WORDS = ("存在", "构成", "属于", "关于", "对于")
# 查询词里默认脱敏的当事人称谓，避免真实姓名外发到第三方法律数据源
NAME_KEYS = ("plaintiff", "defendant", "third_party", "applicant", "respondent")


def _load(path: Path) -> Any:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _clean(text: str) -> str:
    text = re.sub(r"[（(][^）)]*[）)]", "", str(text or ""))
    text = re.sub(r"[，。；、,.;:：!！?？\s]+", "", text)
    return text.strip("的了吗呢")


def _short_focus(focus: str, limit: int = 14) -> str:
    """把争点压成检索短词：剥疑问成分、去标点、限长。"""
    text = re.sub(r"[（(][^）)]*[）)]", "", str(focus or ""))
    for word in SHELL_WORDS:
        text = text.replace(word, "")
    text = re.sub(r"[，。；、,.;:：!！?？\s]+", "", text).strip("的了吗呢")
    for word in LEAD_WORDS:
        if text.startswith(word) and len(text) > len(word) + 1:
            text = text[len(word):]
            break
    return text[:limit] if len(text) > limit else text


def _party_names(summary: Dict[str, Any], party_info: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for key in NAME_KEYS:
        value = str(summary.get(key) or "").strip()
        if value and value not in ("待确认", "待补"):
            names.append(re.split(r"[（(]", value)[0].strip())
    people = party_info.get("当事人") or party_info.get("parties") or []
    if isinstance(people, list):
        for person in people:
            if isinstance(person, dict):
                name = str(person.get("姓名") or person.get("name") or "").strip()
                if name:
                    names.append(name)
    return sorted({n for n in names if len(n) >= 2}, key=len, reverse=True)


def _desensitize(text: str, names: List[str]) -> str:
    for name in names:
        if name and name in text:
            text = text.replace(name, "当事人")
    return text


def _load_context(case_dir: Path) -> Tuple[Dict[str, Any], Dict[str, Any], List[str]]:
    """案由/争点来源优先级：Agent 先落的 findings > 已有 analysis > 单独 summary > spec。"""
    state = case_dir / STATE_DIR
    findings: Dict[str, Any] = {}
    for candidate in (case_dir / RUN_DIR / "agent_findings.json", state / "agent_findings.json"):
        loaded = _load(candidate)
        if isinstance(loaded, dict) and loaded:
            findings = loaded
            break
    analysis = _load(state / "analysis.json")
    if not findings and isinstance(analysis.get("agent_findings"), dict):
        findings = analysis["agent_findings"]
    summary = findings.get("case_summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    if not summary:
        summary = _load(state / "case_summary.json") or {}
    spec = _load(state / "spec.json") or {}
    if not summary.get("cause") and spec.get("cause"):
        summary["cause"] = spec.get("cause")
    focuses: List[str] = []
    for item in (findings.get("dispute_focus") or []):
        if isinstance(item, dict):
            text = str(item.get("focus") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            focuses.append(text)
    if not focuses:
        for item in (summary.get("dispute_focus") or []):
            if str(item).strip():
                focuses.append(str(item).strip())
    return summary, analysis, focuses


def build_queries(cause: str, focuses: List[str], names: List[str]) -> Dict[str, str]:
    shorts = [_short_focus(f) for f in focuses]
    shorts = [s for s in shorts if s]
    cause = cause or "民事纠纷"

    if shorts:
        # 元典语义检索能吃长句：案由 + 前两个争点
        law_q = f"{cause}中，关于{'、'.join(shorts[:2])}，法律是如何规定的？"
        # 企查查法条召回对细节敏感，只问定性争点，避免事实描述稀释语义
        law_q_qcc = f"{cause}中，{shorts[0]}，应当适用哪些法律条文？"
        case_yd = (f"双方就{cause}发生争议，集中在{'、'.join(shorts)}，"
                   f"请求人民法院依法裁判。")
        case_qcc = " ".join([cause] + shorts)
    else:
        law_q = f"{cause}应适用哪些法律规定，责任如何承担？"
        law_q_qcc = law_q
        case_yd = f"双方就{cause}发生争议，请求人民法院依法裁判。"
        case_qcc = cause
    return {
        "law": _desensitize(law_q, names),
        "law_qcc": _desensitize(law_q_qcc, names),
        "case_yuandian": _desensitize(case_yd, names),
        "case_qcc": _desensitize(case_qcc, names),
    }


def search_kinds(case_dir: Path) -> Tuple[str, ...]:
    """需要检索的结果类型：执行案件（zx）只做法条，不做类案检索。

    执行依据为生效判决，争点集中在时效/标的/财产，类案检索命中多为
    『追加被执行人』等无关裁判，无实质参考价值，且多耗 2 次 MCP 调用。
    """
    spec = _load(case_dir / STATE_DIR / "spec.json") or {}
    case_type = str(spec.get("case_type") or "").strip()
    return ("law",) if case_type == "zx" else ("law", "case")


def plan(case_dir: Path, cause: str, focus_arg: List[str], province: str,
         law_limit: int, case_limit: int) -> int:
    case_dir = case_dir.resolve()
    summary, _analysis, focuses = _load_context(case_dir)
    party_info = _load(case_dir / STATE_DIR / "party_info.json")
    names = _party_names(summary, party_info if isinstance(party_info, dict) else {})
    cause = cause or str(summary.get("cause") or "").strip()
    focus_arg = [f for f in (focus_arg or []) if str(f).strip()]
    if focus_arg:
        focuses = focus_arg
    if not cause:
        raise SystemExit("未找到案由：请先跑分析，或用 --cause 显式指定")

    queries = build_queries(cause, focuses, names)
    call_dir = case_dir / STATE_DIR / CALL_SUBDIR
    raw_dir = case_dir / RUN_DIR / RAW_SUBDIR
    raw_dir.mkdir(parents=True, exist_ok=True)  # 预建目录，Agent 直接按 raw_file 落盘

    plan_items: List[Dict[str, Any]] = []
    kinds = search_kinds(case_dir)
    for provider in ys.PROVIDERS:
        for kind in kinds:
            query = {
                ("yuandian", "law"): queries["law"],
                ("qcc", "law"): queries["law_qcc"],
                ("yuandian", "case"): queries["case_yuandian"],
                ("qcc", "case"): queries["case_qcc"],
            }[(provider, kind)]
            limit = law_limit if kind == "law" else case_limit
            out = call_dir / f"{provider}_{kind}.json"
            with contextlib.redirect_stdout(io.StringIO()):
                ys.build_call(provider, kind, query, True, province, None, limit, out)
            plan_items.append({
                "provider": provider,
                "provider_name": ys.PROVIDER_NAME[provider],
                "kind": kind,
                "query": query,
                "tool": ys.EXPECTED[provider][kind],
                "call_file": str(out),
                "raw_file": str(raw_dir / f"{provider}_{kind}.json"),
            })

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cause": cause,
        "focuses": focuses,
        "shorts": [_short_focus(f) for f in focuses],
        "province": ys._province_short(province) if province else "",
        "kinds": list(kinds),
        "desensitized_names": names,
        "queries": queries,
        "calls": plan_items,
        "note": (
            "法条以元典召回为主（本案由下命中更稳），企查查法条仅作交叉印证；"
            "企查查法条常因『劳务/合同』等词泛化召回铁路用工、涉外劳务、船员等无关法规，"
            "入库后若元典与企查查法域明显不一致，报告只保留元典并在交叉校验里记冲突。"
        ),
        "next": [
            "按 calls 逐条执行 MCP（DeferExecuteTool），原始返回原样落 raw_file",
            "跑 ingest-all --case-dir <案件目录> --refresh 批量入库并刷新报告",
        ],
    }
    plan_path = case_dir / STATE_DIR / "search_plan.json"
    _write(plan_path, payload)
    print(json.dumps({
        "ok": True,
        "plan": str(plan_path),
        "cause": cause,
        "queries": queries,
        "calls": [{k: c[k] for k in ("provider", "kind", "tool", "call_file", "raw_file")}
                  for c in plan_items],
    }, ensure_ascii=False, indent=2))
    return 0


def _match_raw(name: str) -> Optional[Tuple[str, str]]:
    low = name.lower()
    provider = next((p for p in ys.PROVIDERS if p in low), None)
    kind = "law" if "law" in low else ("case" if "case" in low else None)
    if provider and kind:
        return provider, kind
    return None


def ingest_all(case_dir: Path, raw_dir: Optional[Path], province: str, refresh: bool,
               findings: Optional[Path]) -> int:
    case_dir = case_dir.resolve()
    raw_dir = (raw_dir or (case_dir / RUN_DIR / RAW_SUBDIR)).resolve()
    if not raw_dir.exists():
        raise SystemExit(f"原始返回目录不存在：{raw_dir}")

    files = sorted(p for p in raw_dir.glob("*.json") if p.is_file())
    if not files:
        raise SystemExit(f"目录内没有 JSON：{raw_dir}")

    results: List[Dict[str, Any]] = []
    missing: List[str] = []
    seen: List[str] = []
    for path in files:
        matched = _match_raw(path.name)
        if not matched:
            results.append({"file": path.name, "skipped": "文件名未含 provider/kind"})
            continue
        provider, kind = matched
        key = f"{provider}.{kind}"
        if kind not in search_kinds(case_dir):
            results.append({"file": path.name, "skipped": f"执行案件跳过 {key}"})
            continue
        if key in seen:
            results.append({"file": path.name, "skipped": f"重复的 {key}"})
            continue
        seen.append(key)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                ys.ingest(kind, path, case_dir, province, provider)
            results.append({"file": path.name, "provider": provider, "kind": kind, "ok": True})
        except SystemExit as exc:
            results.append({"file": path.name, "provider": provider, "kind": kind,
                            "ok": False, "error": str(exc)})
    for provider in ys.PROVIDERS:
        for kind in search_kinds(case_dir):
            if f"{provider}.{kind}" not in seen:
                missing.append(f"{provider}.{kind}")

    out: Dict[str, Any] = {"ok": not missing, "raw_dir": str(raw_dir),
                           "ingested": results, "missing": missing}
    if refresh:
        import analyze_case
        spec = _load(case_dir / STATE_DIR / "spec.json") or {}
        case_type = str(spec.get("case_type") or "sp")
        findings_path = findings or (case_dir / RUN_DIR / "agent_findings.json")
        if not findings_path.exists():
            findings_path = Path()
        refreshed = analyze_case.analyze(case_dir, case_type, findings_path, False)
        out["refresh"] = {"report": refreshed.get("report"),
                          "stats": refreshed.get("stats"),
                          "analysis_source": refreshed.get("analysis_source")}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if not missing else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="一键类案/法条检索（元典 + 企查查）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("plan", help="自动生成 4 份检索调用参数")
    p.add_argument("--case-dir", required=True)
    p.add_argument("--cause", default="", help="覆盖案由，默认取分析结果")
    p.add_argument("--focus", action="append", default=[], help="覆盖争点，可重复")
    p.add_argument("--province", default="")
    p.add_argument("--law-limit", type=int, default=20)
    p.add_argument("--case-limit", type=int, default=12)

    i = sub.add_parser("ingest-all", help="批量 ingest 原始返回并可选刷新报告")
    i.add_argument("--case-dir", required=True)
    i.add_argument("--raw-dir", default="")
    i.add_argument("--province", default="")
    i.add_argument("--refresh", action="store_true", help="入库后重跑分析报告")
    i.add_argument("--findings", default="")

    args = ap.parse_args()
    if args.cmd == "plan":
        return plan(Path(args.case_dir), args.cause, args.focus, args.province,
                    args.law_limit, args.case_limit)
    return ingest_all(Path(args.case_dir), Path(args.raw_dir) if args.raw_dir else None,
                      args.province, args.refresh,
                      Path(args.findings).resolve() if args.findings else None)


if __name__ == "__main__":
    raise SystemExit(main())
