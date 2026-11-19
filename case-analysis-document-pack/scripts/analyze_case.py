#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""分析编排：把聊天证据、律师结构化结论与 MCP 结果合并成报告。

Agent 应先做实质分析，把结论写入 agent_findings.json，再用 --findings 传入。
没有 findings 时只做机械归集，绝不自称完成法律分析。
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import check_completeness

PLACEHOLDER = "待律师补充"
NO_MCP_TEXT = "第三方法律检索未连接或未执行：本章节留空，不输出法条/案号结论。"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _facts_from_chat(chat: Dict[str, Any]) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    for msg in chat.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        facts.append({
            "fact_id": msg.get("id") or f"F-{len(facts) + 1:04d}",
            "text": content,
            "date": str(msg.get("timestamp") or ""),
            "source": {
                "file": msg.get("source_file") or "",
                "locator": msg.get("source_locator") or "",
                "quote": content[:200],
            },
            "confidence": "low" if msg.get("needs_review") else "medium",
            "derived": True,
        })
    return facts


def _merge_findings(findings: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in ("case_summary", "legal_relations", "dispute_focus", "evidence_grid",
                "reference_conclusion", "risks", "questions"):
        value = findings.get(key)
        if value is not None:
            out[key] = value
    facts = findings.get("facts")
    if isinstance(facts, list):
        cleaned: List[Dict[str, Any]] = []
        for item in facts:
            if not isinstance(item, dict):
                continue
            src = item.get("source") or {}
            if not isinstance(src, dict) or not (src.get("file") or src.get("locator")):
                continue
            cleaned.append({
                "fact_id": item.get("fact_id") or f"F-{len(cleaned) + 1:04d}",
                "text": str(item.get("text") or ""),
                "date": str(item.get("date") or ""),
                "source": {
                    "file": str(src.get("file") or ""),
                    "locator": str(src.get("locator") or ""),
                    "quote": str(src.get("quote") or ""),
                },
                "confidence": str(item.get("confidence") or "medium"),
                "derived": False,
            })
        out["facts"] = cleaned
    return out


def _md_table(headers: List[str], rows: List[List[Any]]) -> List[str]:
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        cells = [str(cell).replace("|", "\\|").replace("\n", " ") for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _date_key(date_str: str) -> tuple:
    """把“2026年”“2026-11-18 至 2026-11-19”解析成可排序元组。

    粒度补 0：只有年份的排在该年最前，只有月份的排在月内最前，
    保证粗略时间点在它所属时段之前，不会把“调查日”排到“事实发生”前面。
    """
    import re
    m = re.search(r"(\d{4})(?:[-/年](\d{1,2}))?(?:[-/月](\d{1,2}))?", str(date_str or ""))
    if not m:
        return (9999, 99, 99)
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def _date_granularity(date_str: str) -> str:
    import re
    m = re.search(r"(\d{4})(?:[-/年](\d{1,2}))?(?:[-/月](\d{1,2}))?", str(date_str or ""))
    if not m:
        return "未知"
    if m.group(3):
        return "日"
    if m.group(2):
        return "月"
    return "年"


# 分析框架按案件领域切换；民事用法律关系五步骤，刑事用犯罪构成四要件，行政用合法性审查五要素。
DOMAIN_FRAMEWORKS: Dict[str, Dict[str, Any]] = {
    "civil": {
        "label": "民事法律关系",
        "steps": [
            ("1. 明确争议点及与其相关的法律关系", "step1"),
            ("2. 确定是否产生了法律关系", "step2"),
            ("3. 分析法律关系的性质", "step3"),
            ("4. 分析法律关系的各要素", "step4"),
            ("5. 考察法律关系的变动", "step5"),
        ],
        "focus_headers": ["焦点", "原告观点", "被告观点", "分析", "依据"],
    },
    "criminal": {
        "label": "犯罪构成",
        "steps": [
            ("1. 主体要件（是否达到刑事责任年龄、有无责任能力）", "step1"),
            ("2. 主观要件（故意或过失、目的动机）", "step2"),
            ("3. 客体要件（侵害的法益）", "step3"),
            ("4. 客观要件（危害行为、结果、因果关系）", "step4"),
            ("5. 违法阻却与责任阻却事由", "step5"),
        ],
        "focus_headers": ["焦点", "控方观点", "辩方观点", "分析", "依据"],
    },
    "administrative": {
        "label": "行政行为合法性",
        "steps": [
            ("1. 职权依据（是否超越职权、滥用职权）", "step1"),
            ("2. 事实认定（主要证据是否充分）", "step2"),
            ("3. 法律适用（适用法律法规是否正确）", "step3"),
            ("4. 法定程序（是否遵守法定程序）", "step4"),
            ("5. 裁量适当性（是否明显不当）", "step5"),
        ],
        "focus_headers": ["焦点", "原告观点", "行政机关观点", "分析", "依据"],
    },
}


def _domain_of(findings: Dict[str, Any]) -> str:
    value = str((findings.get("case_summary") or {}).get("case_domain") or "").strip().lower()
    text = value + str((findings.get("case_summary") or {}).get("cause") or "")
    if "刑事" in text or "罪" in text or value == "criminal":
        return "criminal"
    if "行政" in text or value == "administrative":
        return "administrative"
    return "civil"


def _render_timeline(timeline: List[Dict[str, Any]]) -> List[str]:
    """线性时间线：按真实时间正序，同日期合并成一组，粗略时间标出精确度。"""
    if not timeline:
        return ["- 暂无可提取的时间节点"]

    groups: Dict[str, List[Dict[str, Any]]] = {}
    for t in timeline:
        key = str(t.get("date") or "时间待确认").strip()
        groups.setdefault(key, []).append(t)

    ordered = sorted(groups.items(), key=lambda kv: _date_key(kv[0]))
    out: List[str] = []
    for idx, (date, items) in enumerate(ordered):
        granularity = _date_granularity(date)
        suffix = ""
        if granularity == "年":
            suffix = "　（{0}，月份日期未提供，按年度记）".format("仅到年")
        elif granularity == "月":
            suffix = "　（仅到月）"
        out.append(f"**{date}**{suffix}")
        for t in items:
            src = t.get("source") or {}
            loc = f"{src.get('file', '')} {src.get('locator', '')}".strip()
            conf = str(t.get("confidence") or "")
            note = "　⚠ 低置信，需核对原件" if conf in ("low", "medium") else ""
            line = f"- {t.get('text', '')}"
            if loc:
                line += f"　`{loc}`"
            out.append(line + note)
        if idx < len(ordered) - 1:
            out.append("")
    return out


def render_report(analysis: Dict[str, Any], chat: Dict[str, Any], mcp_status: Dict[str, Any]) -> str:
    stats = analysis.get("stats") or {}
    facts = analysis.get("facts") or []
    timeline = analysis.get("timeline") or []
    findings = analysis.get("agent_findings") or {}
    legal = findings.get("legal_relations") or {}
    disputes = findings.get("dispute_focus") or []
    grid = findings.get("evidence_grid") or []
    risks = findings.get("risks") or []
    questions = analysis.get("confirmation_checklist") or []
    laws = (analysis.get("law_search") or {}).get("results") or []
    cases = (analysis.get("case_search") or {}).get("results") or []
    connected = bool(mcp_status.get("connected"))
    reference = findings.get("reference_conclusion") or {}
    if not isinstance(reference, dict):
        reference = {}
    completeness = analysis.get("completeness") or {}
    if not isinstance(completeness, dict):
        completeness = {}
    law_cross = (analysis.get("law_search") or {}).get("cross_check") or {}
    case_cross = (analysis.get("case_search") or {}).get("cross_check") or {}

    out: List[str] = ["# 案件分析报告", ""]
    out += ["## 0. 一眼看懂", ""]
    out += [f"- 事实节点：{stats.get('fact_count', len(facts))} 条",
            f"- 聊天消息：{stats.get('message_count', 0)} 条；截图：{stats.get('image_count', 0)} 张",
            f"- 低置信度项：{stats.get('low_confidence_count', 0)} 条（需人工核对）",
            f"- 争议焦点：{stats.get('dispute_count', len(disputes))} 个",
            f"- 风险/待确认：{stats.get('risk_count', len(risks))} / {len(questions)} 项",
            f"- 案件完整度：{completeness.get('score', '—')}%（critical {completeness.get('critical_count', 0)}）",
            f"- 时间范围：{' 至 '.join(stats.get('date_range') or []) or '待确认'}", ""]

    summary = findings.get("case_summary") or {}
    if not isinstance(summary, dict):
        summary = {}
    out += ["## 1. 基本案情", ""]
    for key, label in (("case_type", "案件类型"), ("cause", "案由"), ("plaintiff", "原告"),
                       ("defendant", "被告"), ("amount", "标的额"), ("court", "受理法院")):
        out.append(f"- {label}：{summary.get(key) or PLACEHOLDER}")
    out.append("")

    # 图片用途标注：Agent 看图后写入 05案件信息/image_labels.json，优先于“需人工核对”
    labels = analysis.get("image_labels") or {}

    out += ["## 2. 材料与来源说明", ""]
    for src in chat.get("sources") or []:
        if not isinstance(src, dict):
            continue
        file = src.get("file") or ""
        label = labels.get(file) or {}
        if label.get("用途"):
            line = f"- `{file}` —— {label['用途']}"
            if label.get("类型"):
                line += f"（{label['类型']}）"
        else:
            line = f"- `{file}`（{src.get('type')}）"
            if src.get("needs_review"):
                line += "，尚未识别，需人工核对"
        out.append(line)
    for warning in chat.get("warnings") or []:
        out.append(f"- 提示：{warning}")
    out.append("")

    out += ["## 3. 案件事实时间线", ""]
    out += _render_timeline(timeline)
    out.append("")

    domain = _domain_of(findings)
    framework = DOMAIN_FRAMEWORKS[domain]
    out += [f"## 4. {framework['label']}分析", "", f"### 4.1 第一阶段：{'五步骤' if domain == 'civil' else '要件审查'}", ""]
    for name, key in framework["steps"]:
        out.append(f"**{name}**：{legal.get(key) or PLACEHOLDER}")
        out.append("")
    out += ["### 4.2 第二阶段：三段论涵摄与参考结论", ""]
    out.append(str(legal.get("syllogism") or PLACEHOLDER))
    out.append("")

    out += ["## 5. 证据三栏表", ""]
    out += _md_table(["主张", "事实依据", "证据及来源", "弱点"],
                     [[g.get("claim", ""), "、".join(g.get("fact_ids") or []),
                       g.get("evidence", ""), g.get("weakness", "")] for g in grid]
                     or [["—", "—", "—", "—"]])
    out.append("")

    out += ["## 6. 争议焦点双列表", ""]
    headers = framework["focus_headers"]
    side_a, side_b = headers[1], headers[2]
    rows = []
    for d in disputes:
        rows.append([
            d.get("focus", ""),
            d.get(side_a, d.get("plaintiff_view", "")),
            d.get(side_b, d.get("defendant_view", "")),
            d.get("analysis", ""),
            "、".join((d.get("fact_ids") or []) + (d.get("law_source_ids") or [])),
        ])
    out += _md_table(headers, rows or [["—"] * len(headers)])
    out.append("")

    out += ["## 7. 法律法规司法解释等相关规定", ""]
    if laws:
        for law in laws:
            providers = "+".join(law.get("providers") or [law.get("source_provider") or "unknown"])
            out.append(f"- [{law.get('source_id')}]（{providers}）{law.get('title')} "
                       f"{law.get('article')}：{law.get('content')}")
    else:
        out.append(NO_MCP_TEXT if not connected else "未检索到可用法条，标待核验；不虚构。")
    if law_cross:
        counts = law_cross.get("counts") or {}
        out.append(f"- 双源核对：元典 {counts.get('yuandian', 0)} 条 / 企查查 {counts.get('qcc', 0)} 条；"
                   f"两源均返回：{'是' if law_cross.get('both_returned') else '否'}；"
                   f"冲突 {len(law_cross.get('conflicts') or [])} 项")
        for conflict in (law_cross.get("conflicts") or [])[:3]:
            out.append(f"- 冲突明细：{conflict.get('key', '')}（{conflict.get('field', '')}，需人工复核）")
        if laws and not law_cross.get("both_returned"):
            out.append("- 提示：当前为单源检索结果，建议连上第二数据源交叉核对后再定稿。")
    out.append("")

    out += ["## 8. 类案检索", ""]
    if cases:
        out += _md_table(["案号", "法院", "裁判日期", "来源", "数据源"],
                         [[c.get("case_number", ""), c.get("court", ""),
                           c.get("decision_date", ""), c.get("source_id", ""),
                           "+".join(c.get("providers") or [c.get("source_provider", "")])] for c in cases])
        if case_cross:
            counts = case_cross.get("counts") or {}
            out.append(f"- 双源核对：元典 {counts.get('yuandian', 0)} 条 / 企查查 {counts.get('qcc', 0)} 条；"
                       f"两源均返回：{'是' if case_cross.get('both_returned') else '否'}；"
                       f"冲突 {len(case_cross.get('conflicts') or [])} 项")
            if not case_cross.get("both_returned"):
                out.append("- 提示：当前为单源检索结果，建议连上第二数据源交叉核对后再定稿。")
    else:
        out.append(NO_MCP_TEXT if not connected else "未检索到符合条件的类案，标待核验；不虚构。")
    out.append("")

    out += ["## 9. 相关程序问题与风险", ""]
    for risk in risks or ["待律师补充"]:
        out.append(f"- {risk}")
    out.append("")

    out += ["## 10. 参考结论与策略建议", ""]
    if reference:
        for key, label in (("conclusion", "参考结论"), ("favorable", "最有利情形"),
                           ("most_likely", "最可能情形"), ("unfavorable", "最不利情形"),
                           ("strategy", "策略建议"), ("evidence_todo", "补证方向")):
            value = reference.get(key)
            if isinstance(value, list):
                out.append(f"**{label}**：")
                out += [f"- {item}" for item in value]
            elif value:
                out.append(f"**{label}**：{value}")
        out.append("")
    else:
        out.append("待律师基于上述事实、法律关系与检索结果判断；本 Skill 不自动给出胜诉率或绝对结论。")
        out.append("")

    out += ["## 11. 案件补全清单", ""]
    out.append(f"- 完整度：{completeness.get('score', '—')}%（{completeness.get('passed', 0)}/{completeness.get('total', 0)}）")
    missing = completeness.get("missing") or []
    critical = [x for x in missing if x.get("severity") == "critical"]
    warnings = [x for x in missing if x.get("severity") != "critical"]
    if critical:
        out += ["", "### 11.1 阻断项（补齐前不生成正式文书）", ""]
        out += [f"- **{x.get('field')}**：{x.get('hint')}" for x in critical]
    if warnings:
        out += ["", "### 11.2 建议补齐", ""]
        out += [f"- {x.get('field')}：{x.get('hint')}" for x in warnings]
    if not missing:
        out.append("关键字段齐全。")
    out.append("")

    out += ["## 12. 其他需要注意的内容", ""]
    out.append("- 低置信度 OCR 项不得作为确定事实，需核对原始截图。")
    out.append("- 法条与类案以第三方检索来源为准；未检索到即标待核验。")
    out.append("")

    out += ["## 13. 律师确认清单", ""]
    for idx, question in enumerate(questions or ["事实与来源", "法律关系", "案由", "法院", "标的额", "诉讼请求", "证据缺口"], 1):
        out.append(f"{idx}. {question}")
    out.append("")

    out += ["## 14. 免责声明", "",
            "本报告为基于所提供材料的辅助分析，不构成正式法律意见；事实以证据原件为准，"
            "法条与类案以检索来源为准，最终结论以律师确认为准。", ""]
    return "\n".join(out)


def analyze(case_dir: Path, case_type: str, findings_path: Path, no_mcp: bool) -> Dict[str, Any]:
    case_dir = case_dir.resolve()
    state_dir = case_dir / "05案件信息"
    chat = _load(state_dir / "chat_evidence.json") if (state_dir / "chat_evidence.json").exists() else {}
    findings = _load(findings_path) if findings_path and findings_path.exists() else {}
    if not isinstance(findings, dict):
        findings = {}

    # 保留 MCP ingest 写入的结果，不被覆盖
    existing = _load(state_dir / "analysis.json") if (state_dir / "analysis.json").exists() else {}
    law_search = existing.get("law_search") if isinstance(existing, dict) else None
    case_search = existing.get("case_search") if isinstance(existing, dict) else None

    mcp_status = _load(state_dir / "mcp_status.json") if (state_dir / "mcp_status.json").exists() else {}
    if no_mcp:
        mcp_status = dict(mcp_status) if isinstance(mcp_status, dict) else {}
        mcp_status["connected"] = False
        mcp_status["note"] = "--no-mcp 指定，法律/案例检索未执行"

    merged = _merge_findings(findings)
    facts = merged.get("facts")
    if not facts:
        facts = _facts_from_chat(chat)
    timeline = []
    for fact in facts:
        timeline.append({
            "date": fact.get("date") or "",
            "text": fact.get("text") or "",
            "source": fact.get("source") or {},
            "confidence": fact.get("confidence") or "medium",
        })
    timeline.sort(key=lambda x: x.get("date") or "")

    agent_findings = findings if findings else {"legal_relations": {}, "dispute_focus": [], "evidence_grid": []}
    disputes = agent_findings.get("dispute_focus") or []
    risks = agent_findings.get("risks") or []
    questions = list(agent_findings.get("questions") or [])
    if len(questions) < 5:
        for default in ("事实与来源是否准确", "法律关系定性与案由", "受理法院与管辖", "标的额与诉讼请求",
                        "证据缺口与补证方向"):
            if default not in questions:
                questions.append(default)

    stats = {
        "fact_count": len(facts),
        "low_confidence_count": sum(1 for f in facts if f.get("confidence") == "low"),
        "evidence_count": len((chat.get("sources") or [])),
        "message_count": (chat.get("stats") or {}).get("message_count", 0),
        "image_count": (chat.get("stats") or {}).get("image_count", 0),
        "date_range": (chat.get("stats") or {}).get("date_range", []),
        "dispute_count": len(disputes),
        "risk_count": len(risks),
    }

    analysis: Dict[str, Any] = {
        "case_dir": str(case_dir),
        "case_type": case_type,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mcp_status": mcp_status,
        "facts": facts,
        "timeline": timeline,
        "agent_findings": agent_findings,
        "stats": stats,
        "law_search": law_search or {"query": "", "results": []},
        "case_search": case_search or {"query": "", "results": [], "date_filter": ">=2021-01-01"},
        "confirmation_checklist": questions,
        "image_labels": _load(case_dir / "05案件信息" / "image_labels.json"),
        "analysis_source": "agent_findings" if findings else "chat_derived_only",
    }
    completeness = check_completeness.check(case_dir, analysis, chat, mcp_status)
    analysis["completeness"] = completeness
    report = render_report(analysis, chat, mcp_status)
    report_path = case_dir / "案件分析报告.md"
    report_path.write_text(report, encoding="utf-8")
    analysis["report_path"] = str(report_path)
    _write(state_dir / "analysis.json", analysis)
    return {
        "ok": True,
        "analysis": str(state_dir / "analysis.json"),
        "report": str(report_path),
        "stats": stats,
        "analysis_source": analysis["analysis_source"],
        "mcp_connected": bool(mcp_status.get("connected")
                              or (law_search or {}).get("results")
                              or (case_search or {}).get("results")),
        "completeness": str(state_dir / "completeness.json"),
        "completeness_ok": completeness.get("ok", False),
        "completeness_score": completeness.get("score", 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成案件分析报告与 analysis.json")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--case-type", choices=["sp", "zx"], default="")
    ap.add_argument("--findings", default="", help="Agent 结构化分析结论 JSON")
    ap.add_argument("--no-mcp", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    case_dir = Path(args.case_dir).resolve()
    if not case_dir.exists():
        raise SystemExit(f"案件目录不存在：{case_dir}")
    spec = json.loads((case_dir / "05案件信息" / "spec.json").read_text(encoding="utf-8")) \
        if (case_dir / "05案件信息" / "spec.json").exists() else {}
    case_type = args.case_type or str(spec.get("case_type") or "sp")
    findings_path = Path(args.findings).resolve() if args.findings else Path()
    result = analyze(case_dir, case_type, findings_path, args.no_mcp)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
