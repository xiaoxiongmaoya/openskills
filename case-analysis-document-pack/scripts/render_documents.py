#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染整套可打印签字 Word；确认门禁不过直接拒绝。

模板一律来自技能自带的实战模板包（`assets/templates/`）：由
`manage_templates.ensure_template()` 复制到 `<工作空间根>/通用件/模板/` 并登记。
**不再生成任何“骨架模板”**，模板缺失直接报错。

- .docx 模板：复制原件 → 末尾追加「填写内容」段 → 填充占位符，表格与版式原样保留。
- .doc 旧格式模板：复制原件，另出 `<同名>-填写内容.docx` 说明应填内容，律师手工填入。

硬规矩：**模板不得修改**。模板文件在 `通用件/模板/` 一律 chmod 444，本脚本只读不写；
所有填充都发生在 `04未签稿/` 的副本上。要改模板内容，改 `assets/templates/` 源再重跑
`pack_templates.py`，不要改副本。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import confirm_gate  # noqa: E402
import doc_slots  # noqa: E402
import manage_templates as mt  # noqa: E402
from docx_helper import append_to_docx, fill_placeholders, find_placeholders, write_docx  # noqa: E402

SIGN_LINE = "（签字/捺印）：____________    日期：____年__月__日"
DOC_FILL_NOTICE = "旧版 .doc 模板无法程序化写入：以下为应填写内容，请对照填入 .doc 原件后打印签字。"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def build_fields(case_dir: Path) -> Dict[str, str]:
    state = case_dir / "05案件信息"
    analysis = _load(state / "analysis.json")
    chat = _load(state / "chat_evidence.json")
    spec = _load(state / "spec.json")
    confirm = _load(state / "analysis_confirm.json")
    findings = (analysis.get("agent_findings") or {}) if isinstance(analysis, dict) else {}
    summary = findings.get("case_summary") or {}
    if not isinstance(summary, dict):
        summary = {}

    def from_spec(key: str, default: str = "（待补充）") -> str:
        value = spec.get(key)
        if isinstance(value, dict):
            return str(value.get("name") or value.get("value") or default)
        return str(value or default)

    plaintiff = str(summary.get("plaintiff") or from_spec("plaintiff"))
    defendant = str(summary.get("defendant") or from_spec("defendant"))
    cause = str(summary.get("cause") or (spec.get("case") or {}).get("cause") or "（待确认）")
    amount = str(summary.get("amount") or (spec.get("case") or {}).get("amount") or "（待确认）")
    court = str(summary.get("court") or "（待确认，管辖由律师确认）")

    claim_text = str(summary.get("claims") or (spec.get("claims") or {}).get("ssqq") or "")
    if not claim_text:
        claim_text = "1. 请求判令被告履行还款义务；\n2. 请求判令被告承担本案诉讼费用。（待律师确认）"

    facts = analysis.get("facts") or []
    fact_lines: List[str] = []
    for idx, fact in enumerate(facts[:30], 1):
        src = fact.get("source") or {}
        fact_lines.append(
            f"{idx}. {fact.get('text', '')}（来源：{src.get('file', '')} {src.get('locator', '')}；"
            f"置信度：{fact.get('confidence', 'medium')}）"
        )
    facts_text = "\n".join(fact_lines) or "（暂无可引用事实，请律师补充）"

    sources = chat.get("sources") or []
    evidence_lines: List[str] = []
    for idx, src in enumerate(sources, 1):
        if isinstance(src, dict):
            evidence_lines.append(f"{idx}. {src.get('file', '')}（{src.get('type', '')}）")
    grid = findings.get("evidence_grid") or []
    for item in grid:
        if isinstance(item, dict):
            evidence_lines.append(f"- {item.get('claim', '')}：{item.get('evidence', '')}")
    evidence_text = "\n".join(evidence_lines) or "（待补充证据清单）"

    agent = spec.get("agent") or {}
    agent_name = str(summary.get("agent_name") or agent.get("name") or "")
    firm = str(summary.get("firm") or agent.get("firm") or "")
    has_agent = bool(summary.get("has_agent") or ((agent.get("enabled") or agent_name or firm) and (agent_name or firm)))

    # 从真实诉状字段里提取身份证号/电话（case_summary 里常是“已提供”这类说明文字）
    import re as _re
    complaint_path = case_dir / "06运行记录" / "complaint_text.json"
    complaint_data = _load(complaint_path) if complaint_path.exists() else {}
    identity = str(complaint_data.get("原告身份信息") or "") if isinstance(complaint_data, dict) else ""
    id_match = _re.search(r"身份证号[:：]\s*([0-9]{17}[\dXx])", identity) or _re.search(r"([0-9]{17}[\dXx])", identity)
    real_id = id_match.group(1) if id_match else ""
    phone_match = _re.search(r"联系电话[:：]\s*(1[3-9]\d{9})", identity) or _re.search(r"(?<!\d)(1[3-9]\d{9})(?!\d)", str(summary.get("plaintiff_phone") or ""))
    real_phone = phone_match.group(1) if phone_match else ""

    def real(value: Any, fallback: str = "") -> str:
        """说明性文字（如“已提供”“待补…”）视为无真实值。"""
        text = str(value or "").strip()
        if not text or text.startswith("已提供") or text.startswith("待补") or "见01原始包" in text:
            return fallback
        return text

    mapping = {
        "原告": plaintiff,
        "被告": defendant,
        "申请执行人": plaintiff,
        "被执行人": defendant,
        "委托人": plaintiff,
        "对方当事人": defendant,
        "受托人": agent_name or "（待补充）",
        "原告身份信息": real(summary.get("plaintiff_id"), real_id),
        "被告身份信息": real(summary.get("defendant_id")),
        "原告住所": real(summary.get("plaintiff_address")),
        "被告住所": real(summary.get("defendant_address")),
        "原告电话": real(summary.get("plaintiff_phone"), real_phone),
        "被告电话": str(summary.get("defendant_phone") or "（待补充）"),
        "住所地": str(summary.get("plaintiff_address") or "（待补充）"),
        "被执行人住所": str(summary.get("defendant_address") or "（待补充）"),
        "受理法院": court,
        "案由": cause,
        "诉讼请求": claim_text,
        "诉讼请求1": claim_text.splitlines()[0] if claim_text else "（待补充）",
        "诉讼请求2": "详见事实与理由",
        "执行请求": claim_text,
        "执行依据": str(summary.get("enforcement_basis") or "（待补充：生效判决书/调解书及生效证明）"),
        "事实与理由": facts_text,
        "要素事实": facts_text,
        "证据清单": evidence_text,
        "当事人": plaintiff,
        "证件号码": real(summary.get("plaintiff_id"), real_id),
        "送达地址": real(summary.get("delivery_address") or summary.get("plaintiff_address")),
        "联系方式": real(summary.get("phone") or summary.get("plaintiff_phone"), real_phone),
        "户名": real(summary.get("account_name"), plaintiff),
        "开户行": real(summary.get("bank_name")),
        "账号": real(summary.get("bank_account")),
        "委托事项": str(summary.get("agency_scope") or "代为提起诉讼、提交材料、参加诉讼活动。"),
        "代理权限": str(summary.get("agency_authority") or "一般授权（待律师确认）。"),
    }
    # 实战模板里的字段占位符（{{身份证号}} 等）与内部字段名不同，这里做一层别名
    mapping.update({
        "身份证号": mapping["证件号码"],
        "手机号": mapping["联系方式"],
        "电话": mapping["联系方式"],
        "银行账号": mapping["账号"],
        "地址": mapping["送达地址"] or mapping["原告住所"],
        "案号": real(summary.get("case_number")),
        "承办律师": mapping["受托人"],
        "公司名称": real(summary.get("company_name")),
        "律师事务所": real(summary.get("firm")),
        "出生日期": real(summary.get("birth_date")),
    })
    mapping["_meta"] = json.dumps({
        "plaintiff": plaintiff, "defendant": defendant, "case_type": str(confirm.get("case_type") or "sp"),
        "complaint_format": str(confirm.get("complaint_format") or ""), "has_agent": has_agent,
        "court": court, "cause": cause, "amount": amount, "agent": agent_name, "firm": firm,
    }, ensure_ascii=False)
    return mapping


def _meta(fields: Dict[str, str]) -> Dict[str, Any]:
    return json.loads(fields.get("_meta") or "{}")


def _emit(template: Path, target_stem: Path, title: str, paras: List[str],
          fields: Dict[str, str]) -> Dict[str, Any]:
    """按模板扩展名选择注入方式，返回实际落盘信息。

    硬规矩：模板只读，任何写入只发生在传进来的副本路径上。
    """
    target_stem = target_stem.resolve()
    template = template.resolve()
    if template == target_stem or template.parent == target_stem.parent:
        raise SystemExit(f"拒绝写入模板目录，只能写副本：{target_stem}")

    if template.suffix.lower() == ".docx" and zipfile.is_zipfile(template):
        target = target_stem.with_suffix(".docx")
        shutil.copyfile(template, target)
        try:
            append_to_docx(target, "填写内容（可打印签字）", paras)
            fill_placeholders(target, fields, blank_missing=True)
        except Exception as exc:  # 模板结构异常：保留原件，填写内容另出
            companion = target_stem.with_name(target_stem.name + "-填写内容.docx")
            write_docx(companion, f"{title}（填写内容）", paras, notice=f"模板注入失败：{exc}")
            return {"file": target, "companion": companion, "mode": "docx_copy_only"}
        return {"file": target, "companion": None, "mode": "docx_injected"}

    target = target_stem.with_suffix(".doc")
    shutil.copyfile(template, target)
    companion = target_stem.with_name(target_stem.name + "-填写内容.docx")
    write_docx(companion, f"{title}（填写内容）", paras, notice=DOC_FILL_NOTICE)
    return {"file": target, "companion": companion, "mode": "doc_manual_fill"}


def render(case_dir: Path, case_type: str, only_complaint: bool = False,
           only: set[str] | None = None) -> Dict[str, Any]:
    case_dir = case_dir.resolve()
    gate = confirm_gate.check(case_dir)
    if not gate["ok"]:
        raise SystemExit("确认门禁未通过：" + "；".join(gate["problems"]))
    root = case_dir.parent.parent
    template_dir = root / "通用件" / "模板"
    template_dir.mkdir(parents=True, exist_ok=True)
    registry = mt._load_registry(template_dir)
    fields = build_fields(case_dir)
    meta = _meta(fields)
    confirm = _load(case_dir / "05案件信息" / "analysis_confirm.json")
    fmt = str(confirm.get("complaint_format") or "ordinary")
    drafts = case_dir / "04未签稿"
    drafts.mkdir(parents=True, exist_ok=True)

    docs: List[Dict[str, Any]] = []

    BLANK_TOKENS = ("（待补充）", "(待补充)", "（待确认）", "(待确认)", "待补充", "待确认",
                    "（待补", "详见事实与理由", "暂无", "（暂无")

    def clean(value: Any) -> str:
        import re as _re
        text = str(value or "").strip()
        if text in BLANK_TOKENS:
            return ""
        # 值是“（待补充）”这类纯占位括号（可带尾部标点）也算空
        if _re.fullmatch(r"[（(]?\s*(待补充|待确认|待补|暂无)\s*[）)]?[\s。；;]*", text):
            return ""
        return text

    def keep(paras: List[str]) -> List[str]:
        """过滤掉没值的填写行：一行里所有“标签：值”都是占位/空就整行不写。

        保留签名/捺印/盖章行。按“标签：值”逐段解析，值里若夹带下一个标签则正确切开。
        """
        import re as _re
        # 标签：中文或常见字段名，值：非“：”字符；用 lookahead 在“空格+新标签：”处切断
        pair_re = _re.compile(r"([\u4e00-\u9fa5A-Za-z/（）()]+)[：:]\s*([^：:]*?)(?=\s{2,}[\u4e00-\u9fa5A-Za-z/（）()]+[：:]|$)")
        out: List[str] = []
        for p in paras:
            if not p.strip():
                continue
            if any(t in p for t in ("签字", "捺印", "盖章")):
                out.append(p)
                continue
            pairs = [(lab.strip(), clean(val)) for lab, val in pair_re.findall(p)]
            if pairs:
                kept = [(lab, val) for lab, val in pairs if val]
                if not kept:
                    continue
                out.append("    ".join(f"{lab}：{val}" for lab, val in kept))
            elif clean(p):
                out.append(p)
        return out

    def add(name: str, key: str, out_stem: str, title: str, paras: List[str]) -> None:
        if only is not None and key not in only:
            return
        entry = mt.ensure_template(root, key, registry)
        info = mt.resolve(key)
        stem = (drafts / out_stem).resolve()
        if drafts.resolve() not in stem.parents:
            raise SystemExit(f"产物必须落在 04未签稿/，拒绝写入：{stem}")
        emitted = _emit(Path(entry["file"]), stem, title, keep(paras), fields)
        item = {
            "name": name,
            "file": f"04未签稿/{emitted['file'].name}",
            "slot": doc_slots.slot_name_for(name, case_type),
            "slot_code": doc_slots.slot_code_for(name, case_type),
            "template": f"{info['title']}（{info['src']}）",
            "template_key": key,
            "template_status": info["status"],
            "template_origin": str(info["path"]),
            "inject_mode": emitted["mode"],
            "sign_positions": [SIGN_LINE],
            "status": "generated",
        }
        if emitted["companion"]:
            item["fill_guide"] = f"04未签稿/{emitted['companion'].name}"
        docs.append(item)

    def complaint_mapping() -> Dict[str, str]:
        """诉状占位符取值：优先 Agent 写入的 complaint_text.json，其次 fields。

        无值就是空串（留空），不写“待补/待确认/（待补充）”这类占位文案。
        """
        path = case_dir / "06运行记录" / "complaint_text.json"
        agent = _load(path) if path.exists() else {}
        if not isinstance(agent, dict):
            agent = {}
        base = dict(fields)
        # 旧键兼容：把案子专用的 A/B、诉请1/2 归并回通用键
        if agent.get("被告身份信息A") or agent.get("被告身份信息B"):
            parts = [str(agent.get("被告身份信息A") or "").strip(),
                     str(agent.get("被告身份信息B") or "").strip()]
            agent.setdefault("被告身份信息", "，".join(p for p in parts if p))
        if agent.get("诉讼请求1") or agent.get("诉讼请求2"):
            lines = [str(agent.get("诉讼请求1") or "").strip(),
                     str(agent.get("诉讼请求2") or "").strip()]
            agent.setdefault("诉讼请求", "\n".join(x for x in lines if x))
        merged = {**base, **{k: v for k, v in agent.items() if isinstance(v, str)}}
        # 无值/占位文案一律留空
        blank_tokens = ("（待补充）", "(待补充)", "（待确认）", "(待确认)",
                        "待补充", "待确认", "（待补", "详见事实与理由")
        clean: Dict[str, str] = {}
        for k, v in merged.items():
            text = str(v or "").strip()
            if text in blank_tokens:
                text = ""
            clean[k] = text
        return clean

    def add_filled(name: str, key: str, out_stem: str, title: str) -> None:
        """按模板占位符直接填值：保留模板全部原文与版式，不追加重复段落。"""
        if only is not None and key not in only:
            return
        entry = mt.ensure_template(root, key, registry)
        info = mt.resolve(key)
        template = Path(entry["file"])
        target = drafts / (out_stem + ".docx")
        if template.suffix.lower() == ".docx" and zipfile.is_zipfile(template):
            shutil.copyfile(template, target)
            fill_placeholders(target, complaint_mapping(), blank_missing=True)
            mode = "docx_placeholder_filled"
        else:
            shutil.copyfile(template, target.with_suffix(".doc"))
            mode = "doc_manual_fill"
        docs.append({
            "name": name,
            "file": f"04未签稿/{target.name}",
            "slot": doc_slots.slot_name_for(name, case_type),
            "slot_code": doc_slots.slot_code_for(name, case_type),
            "template": f"{info['title']}（{info['src']}）",
            "template_key": key,
            "template_status": info["status"],
            "template_origin": str(info["path"]),
            "inject_mode": mode,
            "sign_positions": [SIGN_LINE],
            "status": "generated",
        })

    def complaint_key() -> str:
        if fmt == "elements":
            picked = mt.complaint_template(str(meta.get("cause") or ""))
            if picked:
                return picked
        return "complaint_ordinary"

    if case_type == "zx":
        add("强制执行申请书", "enforcement", "01-强制执行申请书", "强制执行申请书", [
            f"申请执行人：{fields['申请执行人']}    被执行人：{fields['被执行人']}",
            f"## 执行依据\n{fields['执行依据']}",
            f"## 执行请求\n{fields['执行请求']}",
            f"## 事实与理由\n{fields['事实与理由']}",
            f"此致 {fields['受理法院']}", SIGN_LINE,
        ])
        if not only_complaint:
            add("收款账户确认书", "payee_account", "06-收款账户确认书", "收款账户确认书", [
                f"申请执行人/领款人：{fields['申请执行人']}",
                f"开户行：{fields['开户行']}    账号：{fields['账号']}", SIGN_LINE,
            ])
    else:
        key = complaint_key()
        label = "要素式起诉状" if key.startswith("complaint_elements") else "起诉状"
        tpl_file = Path(mt.ensure_template(root, key, registry)["file"])
        complaint_stem = "01-起诉状-普通版" if fmt != "elements" else "01-起诉状-要素式"
        if find_placeholders(tpl_file):
            # 通用占位符模板：直接填，保留原文版式，无值留空
            add_filled(label, key, complaint_stem, "民事起诉状")
        else:
            m = complaint_mapping()
            add(label, key, complaint_stem, "民事起诉状", [
                f"原告：{m['原告']}    被告：{m['被告']}",
                f"## 诉讼请求\n{m['诉讼请求']}",
                f"## 事实与理由\n{m['事实与理由']}",
                f"此致 {m['受理法院']}", SIGN_LINE,
            ])
        if not only_complaint:
            add("账户确认书", "account", "06-账户确认书", "诉讼费退费及收取金钱债权账户确认书", [
                f"户名：{fields['户名']}", f"开户行：{fields['开户行']}    账号：{fields['账号']}", SIGN_LINE,
            ])

    if not only_complaint:
        add("送达地址确认书", "delivery", "05-送达地址确认书", "送达地址确认书", [
            f"受送达人：{fields['当事人']}    证件号码：{fields['证件号码']}",
            f"送达地址：{fields['送达地址']}",
            "是否接受电子送达：□是 □否    电子邮箱/手机号：" + fields["联系方式"],
            SIGN_LINE,
        ])
        add("证据目录", "evidence", "04-证据目录", "证据目录", [
            f"提交人：{fields['原告']}", f"## 证据清单\n{fields['证据清单']}", SIGN_LINE,
        ])
        if meta.get("has_agent"):
            add("授权委托书", "authorization", "03-授权委托书", "授权委托书", [
                f"委托人：{fields['委托人']}    受托人：{fields['受托人']}",
                f"## 委托事项\n{fields['委托事项']}",
                f"## 代理权限\n{fields['代理权限']}",
                f"委托人{SIGN_LINE}    受托人（签字）：____________",
            ])

    mt._save_registry(template_dir, registry)

    client_items = (
        ["原告身份证复印件/营业执照", "被告身份信息", "证据原件或复印件", "签字盖章页"]
        if case_type == "sp" else
        ["生效判决书/调解书/仲裁裁决及生效证明", "申请人身份证明", "被执行人身份/工商信息",
         "银行卡或基本户信息", "签字盖章页"]
    )
    if meta.get("has_agent"):
        client_items.append("律师执业证复印件")
        client_items.append("律师事务所盖章函")

    lines = ["# 提交材料清单", "", f"- 案件类型：{case_type}", f"- 案由：{fields['案由']}",
             f"- 标的额：{meta.get('amount') or '（待确认）'}", ""]
    lines.append("## 本 Skill 已生成（可打印签字）")
    for doc in docs:
        lines.append(f"- {doc['name']}：`{doc['file']}`（槽位 {doc['slot_code']} {doc['slot']}）")
    guides = [d for d in docs if d.get("fill_guide")]
    if guides:
        lines += ["", "## 旧版 .doc 模板：需人工填写"]
        for doc in guides:
            lines.append(f"- `{doc['file']}` → 应填内容见 `{doc['fill_guide']}`")
    lines += ["", "## 需客户/律师补充 or 签字盖章"]
    lines += [f"- {item}" for item in client_items]
    extra = doc_slots.extra_copy_for("账户确认书")
    if extra:
        lines.append(f"- 账户确认书需双投到「{extra}」槽位，由后续立案 Skill 处理；本 Skill 不上传。")
    lines += ["", "## 明确不做", "- 客户确认表（客户不会签，确认留痕在 analysis_confirm.json）",
              "- 财产线索表（默认不做，明确要求时再生成）"]
    if only_complaint:
        lines.append("- 本次按用户要求仅生成诉状，其他材料待后续指令")
    lines.append("")
    checklist = drafts / "提交材料清单.md"
    checklist.write_text("\n".join(lines), encoding="utf-8")

    manifest = {
        "case_dir": str(case_dir),
        "case_type": case_type,
        "complaint_format": fmt,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "documents": docs,
        "submission_checklist": f"04未签稿/{checklist.name}",
        "template_registry": str(template_dir / "来源登记.json"),
        "has_agent": bool(meta.get("has_agent")),
    }
    out = case_dir / "05案件信息" / "document_manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "manifest": str(out),
        "documents": [d["file"] for d in docs],
        "checklist": f"04未签稿/{checklist.name}",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成整套可打印签字 Word")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--case-type", choices=["sp", "zx"], default="")
    ap.add_argument("--only-complaint", action="store_true", help="只生成诉状，不出整套材料")
    ap.add_argument("--only", default="", help="只生成指定模板 key，逗号分隔，如 delivery,account")
    args = ap.parse_args()

    case_dir = Path(args.case_dir).resolve()
    if not case_dir.exists():
        raise SystemExit(f"案件目录不存在：{case_dir}")
    case_type = args.case_type or str(_load(case_dir / "05案件信息" / "analysis_confirm.json").get("case_type") or "sp")
    only = {k.strip() for k in args.only.split(",") if k.strip()} if args.only else None
    result = render(case_dir, case_type, only_complaint=args.only_complaint, only=only)
    if args.only_complaint:
        result["documents"] = [d for d in result["documents"] if "起诉状" in d or "申请书" in d]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
