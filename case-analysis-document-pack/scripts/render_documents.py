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
import party_info as pi  # noqa: E402
from docx_helper import (append_to_docx, fill_complaint_rows, fill_placeholders,  # noqa: E402
                         find_placeholders, qualify_role_placeholders, write_docx)

SIGN_LINE = "（签字/捺印）：____________    日期：____年__月__日"
DOC_FILL_NOTICE = "旧版 .doc 模板无法程序化写入：以下为应填写内容，请对照填入 .doc 原件后打印签字。"

# 按需文书（SKILL.md 的「可选」清单）：模板 key -> (输出文件名, 文书标题, 是否追加填写段)
# 追加与否取决于模板是否自带正文占位符；自带就只填不追加，空白模板才追加。
EXTRA_DOCS: Dict[str, tuple] = {
    "preservation": ("07-财产保全申请书", "财产保全申请书", False),
    "preservation_withdraw": ("08-撤回财产保全申请书", "撤回财产保全申请书", False),
    "postpone_hearing": ("09-延期开庭申请书", "延期开庭申请书", False),
    # 下面几份是法院一页式空白表格，正文由律师按表格手填，追加段落只会重复
    "online_hearing": ("10-线上开庭申请书", "线上开庭申请书", False),
    "withdraw_suit_doc": ("11-撤诉申请书", "撤诉申请书", True),
    "add_defendant_doc": ("12-追加被告申请书", "追加被告申请书", True),
    "enforcement_close": ("13-执行结案申请书", "执行结案申请书", False),
    "net_inquiry_doc": ("14-网络查控申请书", "网络查控申请书", True),
    "mediation": ("15-先行调解申请书", "先行调解申请书", False),
    "statement_general": ("16-情况说明", "情况说明", False),
    "statement_filing": ("17-立案情况说明", "立案情况说明", False),
    "statement_cash": ("18-现金交付情况说明", "现金交付情况说明", False),
    "debt_transfer": ("19-债权转移确认书", "债权转移确认书", False),
    "labor_entrust": ("20-劳务诉讼委托书", "劳务欠款纠纷诉讼委托书", False),
}

# 一页式空白表格：输出原件，正文由律师手填，清单里明确提示
EXTRA_HAND_FILL = {"online_hearing", "mediation", "debt_transfer", "labor_entrust"}

# extra_docs.json 里允许按文书覆盖的正文槽
EXTRA_CONTENT_KEYS = ("申请事项", "事实与理由", "情况说明", "执行请求", "执行依据")

# 有固定程式的按需文书：给了默认申请事项，避免把“判令支付”这类诉请误填进保全/执行申请。
# 需要个案表述的（延期开庭、撤诉、情况说明等）不设默认，一律由 extra_docs.json 提供。
EXTRA_DEFAULT_APPLY: Dict[str, str] = {
    "preservation": "请求依法查封、扣押或冻结被申请人名下价值{N}的财产，保全费用由被申请人承担。",
    "preservation_withdraw": "请求撤回本案财产保全申请。",
    "enforcement_close": "请求对本案执行程序作结案处理。",
    "postpone_hearing": "请求延期开庭审理本案。",
    "online_hearing": "请求准许申请人以线上方式参加本案庭审。",
    "withdraw_suit_doc": "请求准许撤回本案起诉。",
    "net_inquiry_doc": "请求依法查询被申请人名下财产。",
    "mediation": "请求对本案先行调解。",
}


import re  # noqa: E402


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _amount_display(amount: str) -> str:
    """把标的额规整成诉状用语：'12000元' -> '劳务报酬人民币12000元' 的金额部分。"""
    text = str(amount or "").strip()
    # 已是“人民币X元”或纯数字都统一成“人民币X元”
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*元?", text)
    if m:
        return f"人民币{m.group(1)}元"
    return text


def _debt_label(cause: str) -> str:
    """按案由给出诉请里的债务性质措辞，让「支付拖欠的X」读起来像人话。"""
    c = str(cause or "")
    rules = (
        (("劳务",), "劳务报酬"),
        (("劳动", "追索劳动报酬"), "劳动报酬"),
        (("买卖", "货款", "供销"), "货款"),
        (("借贷", "借款"), "借款本金"),
        (("租赁",), "租金"),
        (("建设工程", "工程款", "施工"), "工程款"),
        (("承揽", "加工"), "报酬"),
        (("运输",), "运输费"),
        (("服务",), "服务费"),
        (("不当得利",), "款项"),
    )
    for keys, label in rules:
        if any(k in c for k in keys):
            return label
    return "款项"


def _has_lawyer_fee_evidence(case_dir: Path) -> bool:
    """证据包是否含律师费支付凭证（据此决定是否把律师费列入诉请，不靠手写）。"""
    pkg = _load(case_dir / "05案件信息" / "evidence_package.json")
    for entry in (pkg.get("entries") or []):
        if not isinstance(entry, dict) or entry.get("存疑"):
            continue
        purpose = str(entry.get("证明目的") or "")
        if any(kw in purpose for kw in ("律师费", "代理费", "律师服务费", "维权费用")):
            return True
    return False


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
    # 诉状「此致」栏只写法院名，剥掉“（被告住所地，德阳广汉）”这类内部说明
    court = re.sub(r"[（(][^（）()]*[)）]\s*$", "", court).strip() or court

    # 诉讼请求：骨架由规则固定生成，保证每次出稿措辞一致（Agent 不再手写正文）。
    # findings.claims 只用于补充规则未覆盖的额外请求，不覆盖骨架措辞。
    raw_claims = summary.get("claims") or (spec.get("claims") or {}).get("ssqq") or []
    if isinstance(raw_claims, str):
        extra_candidates = [c.strip() for c in re.split(r"[\n；;]", raw_claims) if c.strip()]
    elif isinstance(raw_claims, list):
        extra_candidates = [str(c).strip() for c in raw_claims if str(c).strip()]
    else:
        extra_candidates = []

    claim_items: List[str] = []
    if amount and amount not in ("（待确认）", "（待补充）", "（待确认金额）"):
        claim_items.append(f"判令被告向原告支付拖欠的{_debt_label(cause)}{_amount_display(amount)}")
    if _has_lawyer_fee_evidence(case_dir):
        claim_items.append("判令被告承担原告为实现债权支出的律师费（以票据金额为准）")
    claim_items.append("本案诉讼费用由被告承担")

    # 追加规则未覆盖的额外请求（如违约金、确认之诉）：已含支付/承担/诉讼费等骨架语义的丢弃
    skeleton_kw = ("支付", "拖欠", "律师费", "诉讼费", "诉讼费用", "承担本案")
    for c in extra_candidates:
        c = re.sub(r"^\s*\d+[.、．]\s*", "", c).strip().rstrip("；;。")
        if c and not any(k in c for k in skeleton_kw):
            claim_items.append(c)

    # 前 n-1 条用分号，最后一条用句号；编号统一重排
    cleaned_claims = [c.rstrip("；;。") for c in claim_items if c.strip()]
    claim_text = "\n".join(
        f"{i}. {c}{'；' if i < len(cleaned_claims) else '。'}"
        for i, c in enumerate(cleaned_claims, 1)
    )
    # 兼容读取分条的模板占位符（{{诉讼请求1}}/{{诉讼请求2}}…）
    claim_parts = [
        f"{i}. {c}{'；' if i < len(cleaned_claims) else '。'}"
        for i, c in enumerate(cleaned_claims, 1)
    ]

    # 事实与理由：由 facts 自动串成叙事段，剔除内部来源/置信度标注；无事实则留空。
    facts = analysis.get("facts") or []
    narrative: List[str] = []
    for fact in facts[:40]:
        text = str(fact.get("text") or "").strip()
        if text:
            narrative.append(text if text.endswith(("。", "；", "！")) else text + "。")
    facts_text = "".join(narrative) or ""

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

    # 当事人真实信息：优先 party_info.json（由材料/OCR 提取并留痕），支持多当事人。
    # case_summary 里常是“已提供”这类说明文字，不能当真值用。
    import re as _re
    pinfo = pi.load(case_dir)
    main_pl = pinfo.get("primary_plaintiff") or {}
    main_def = pinfo.get("primary_defendant") or {}
    complaint_path = case_dir / "06运行记录" / "complaint_text.json"
    complaint_data = _load(complaint_path) if complaint_path.exists() else {}
    identity = str(complaint_data.get("原告身份信息") or "") if isinstance(complaint_data, dict) else ""
    id_match = _re.search(r"身份证号[:：]\s*([0-9]{17}[\dXx])", identity) or _re.search(r"([0-9]{17}[\dXx])", identity)
    phone_match = _re.search(r"联系电话[:：]\s*(1[3-9]\d{9})", identity) or _re.search(r"(?<!\d)(1[3-9]\d{9})(?!\d)", str(summary.get("plaintiff_phone") or ""))
    real_id = str(main_pl.get("id") or (id_match.group(1) if id_match else ""))
    real_phone = str(main_pl.get("phone") or (phone_match.group(1) if phone_match else ""))
    delivery_address = str(main_pl.get("address") or "")
    account_name = str(main_pl.get("account_name") or "")
    bank_name = str(main_pl.get("bank") or "")
    bank_account = str(main_pl.get("account") or "")
    # 多当事人：模板姓名栏用顿号连接
    if pinfo.get("multi"):
        if pinfo.get("plaintiffs"):
            plaintiff = pi.join_names(pinfo["plaintiffs"])
        if pinfo.get("defendants"):
            defendant = pi.join_names(pinfo["defendants"])

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
        "诉讼请求1": claim_parts[0] if len(claim_parts) > 0 else "",
        "诉讼请求2": claim_parts[1] if len(claim_parts) > 1 else "",
        "诉讼请求3": claim_parts[2] if len(claim_parts) > 2 else "",
        "诉讼请求4": claim_parts[3] if len(claim_parts) > 3 else "",
        "执行请求": claim_text,
        "执行依据": str(summary.get("enforcement_basis") or "（待补充：生效判决书/调解书及生效证明）"),
        "事实与理由": facts_text,
        "要素事实": facts_text,
        "证据清单": evidence_text,
        "当事人": plaintiff,
        "证件号码": real_id or real(summary.get("plaintiff_id")),
        "送达地址": delivery_address or real(summary.get("delivery_address") or summary.get("plaintiff_address")),
        "联系方式": real_phone or real(summary.get("phone") or summary.get("plaintiff_phone")),
        "户名": account_name or real(summary.get("account_name"), plaintiff),
        "开户行": bank_name or real(summary.get("bank_name")),
        "账号": bank_account or real(summary.get("bank_account")),
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

    # 普通版诉状按当事人分别占位（原告/被告各一套），性别、出生日期可从身份证号推。
    def _id_birth(value: Any) -> str:
        text = str(value or "").strip()
        if len(text) == 18 and text[:17].isdigit():
            return f"{text[6:10]}年{int(text[10:12])}月{int(text[12:14])}日"
        return ""

    def _id_gender(value: Any) -> str:
        text = str(value or "").strip()
        if len(text) == 18 and text[16].isdigit():
            return "男" if int(text[16]) % 2 else "女"
        return ""

    for tag, party in (("原告", main_pl), ("被告", main_def)):
        pid = str(party.get("id") or "")
        gender = str(party.get("gender") or _id_gender(pid))
        ethnic = str(party.get("ethnicity") or "")
        birth = str(party.get("birth_date") or _id_birth(pid))
        addr = str(party.get("address") or "")
        phone = str(party.get("phone") or "")
        mapping[f"{tag}性别"] = gender
        mapping[f"{tag}民族"] = ethnic
        mapping[f"{tag}出生日期"] = birth
        mapping[f"{tag}地址"] = addr
        mapping[f"{tag}身份证号"] = pid
        mapping[f"{tag}手机号"] = phone
        # 分段占位：缺值整段消失，不留下「，族，」这种断裂
        mapping[f"{tag}性别段"] = f"，{gender}" if gender else ""
        mapping[f"{tag}民族段"] = f"，{ethnic}族" if ethnic else ""
        mapping[f"{tag}出生段"] = f"，{birth}出生" if birth else ""
        mapping[f"{tag}地址段"] = f"，住{addr}" if addr else ""
        mapping[f"{tag}证件段"] = f"，身份证号：{pid}" if pid else ""
        mapping[f"{tag}电话段"] = f"，联系电话：{phone}" if phone else ""
    mapping.setdefault("性别", mapping["原告性别"])
    mapping.setdefault("民族", mapping["原告民族"])
    # 单值通用键（供执行/保全/情况说明等模板使用），无来源就留空
    if not mapping.get("出生日期"):
        mapping["出生日期"] = mapping.get("原告出生日期", "")
    if not mapping.get("申请事项"):
        mapping["申请事项"] = mapping.get("执行请求", "") or mapping.get("诉讼请求", "")
    if not mapping.get("情况说明"):
        mapping["情况说明"] = mapping.get("事实与理由", "")
    mapping.setdefault("微信号", "")
    mapping["_meta"] = json.dumps({
        "plaintiff": plaintiff, "defendant": defendant, "case_type": str(confirm.get("case_type") or "sp"),
        "complaint_format": str(confirm.get("complaint_format") or ""), "has_agent": has_agent,
        "court": court, "cause": cause, "amount": amount, "agent": agent_name, "firm": firm,
    }, ensure_ascii=False)
    _sanitize_fields(mapping, case_dir)
    _expand_aliases(mapping, case_dir)
    return mapping


def _expand_aliases(mapping: Dict[str, str], case_dir: Path) -> None:
    """按模板实际用词补齐同义键，避免换个说法就填不上、或串用别人的信息。

    实战模板对同一含义有多种写法（手机号/电话/联系电话/联系手机号码，
    开户行/开户银行，账号/收款账号/银行账号）。这里统一补齐，只填空位不覆盖已有值。
    """
    def put(key: str, value: Any) -> None:
        if value and not mapping.get(key):
            mapping[key] = str(value)

    pl_phone = mapping.get("联系方式") or mapping.get("手机号") or ""
    put("联系电话", pl_phone)
    put("联系手机号码", pl_phone)
    put("开户银行", mapping.get("开户行"))
    put("收款账号", mapping.get("账号") or mapping.get("银行账号"))
    put("当事人", mapping.get("原告"))

    try:
        pinfo = pi.load(case_dir)
    except Exception:
        pinfo = {}
    pl = pinfo.get("primary_plaintiff") or {}
    raw = pl.get("raw") if isinstance(pl.get("raw"), dict) else {}
    put("邮编", raw.get("邮编"))
    put("电子邮箱", raw.get("电子邮箱") or raw.get("邮箱"))
    put("联行号", raw.get("联行号") or pl.get("联行号"))
    put("律师事务所", raw.get("律师事务所") or mapping.get("律师事务所"))
    put("承办律师", raw.get("承办律师") or mapping.get("承办律师"))
    put("公司名称", raw.get("公司名称"))

    # 角色组：无角色键被 qualify_role_placeholders 改成「原告X/被告X」后要能取到值
    for role, bucket in (("原告", "plaintiffs"), ("被告", "defendants")):
        people = pinfo.get(bucket) or []
        person = (people or [{}])[0] or {}
        put(f"{role}姓名", person.get("name"))
        put(f"{role}性别", person.get("sex"))
        put(f"{role}民族", person.get("nation"))
        put(f"{role}出生日期", person.get("birth"))
        put(f"{role}地址", person.get("address"))
        put(f"{role}身份证号", person.get("id"))
        put(f"{role}手机号", person.get("phone"))
        put(f"{role}电话", person.get("phone"))
        # 带序号的键（原告1姓名/被告2身份证号…）：任何预留多行的模板都能取到人，
        # 不再只有诉状走 complaint_row_mapping 时才可用。
        for idx, p in enumerate(people, 1):
            for field, key in (("姓名", "name"), ("性别", "sex"), ("民族", "nation"),
                               ("出生日期", "birth"), ("地址", "address"),
                               ("身份证号", "id"), ("电话", "phone"), ("手机号", "phone")):
                put(f"{role}{idx}{field}", p.get(key))

    # 通用键从角色键回填：角色键来自 party_info（含 OCR 落库的民族/出生日期），更可靠。
    # 早于 _expand_aliases 生成的通用键此时还是空的，必须在角色键补齐后再回填一次。
    for common, rolekey in (("民族", "原告民族"), ("性别", "原告性别"),
                            ("出生日期", "原告出生日期")):
        if not mapping.get(common) and mapping.get(rolekey):
            mapping[common] = mapping[rolekey]

    # 段落键（供 {{原告民族段}} 这类模板）按回填后的值重算
    for tag in ("原告", "被告"):
        sex = mapping.get(f"{tag}性别") or ""
        ethnic = mapping.get(f"{tag}民族") or ""
        birth = mapping.get(f"{tag}出生日期") or ""
        addr = mapping.get(f"{tag}地址") or ""
        pid = mapping.get(f"{tag}身份证号") or ""
        phone = mapping.get(f"{tag}电话") or ""
        mapping[f"{tag}性别段"] = f"，{sex}" if sex else ""
        mapping[f"{tag}民族段"] = f"，{ethnic}族" if ethnic else ""
        mapping[f"{tag}出生段"] = f"，{birth}出生" if birth else ""
        mapping[f"{tag}地址段"] = f"，住{addr}" if addr else ""
        mapping[f"{tag}证件段"] = f"，身份证号：{pid}" if pid else ""
        mapping[f"{tag}电话段"] = f"，联系电话：{phone}" if phone else ""

    # 立案后才知道、或由当事人手写的字段，保持空白
    for key in ("日期", "案号", "微信号"):
        mapping.setdefault(key, "")


# 内部备注/占位文案特征：这类文字绝不能印到交付给法院或当事人的文书上
_NOTE_MARKERS = ("待律师", "待确认", "待补充", "待补", "见聊天记录", "备案登记",
                 "需律师", "据类案", "以详情为准", "OCR", "截图", "按规则")

# 需要净化的字段类别：只针对身份、联系、账户、代理信息；正文一律不动
_SANITIZE_TARGETS = ("身份", "住址", "地址", "电话", "手机", "开户", "账号", "户名",
                     "受托人", "委托人", "承办律师", "当事人", "民族", "性别",
                     "出生", "案由", "法院", "证件", "邮编", "邮箱", "联行号")


def _sanitize_fields(mapping: Dict[str, str], case_dir: Path) -> None:
    """就地清洗字段：剥掉内部备注、清空占位文案，并用当事人信息重建身份段落。

    实战里 findings 会写“待律师提供（原告身份证号仅见于截图…）”这类内部说明，
    一旦被模板占位符带进文书就是事故，所以统一在出稿前做一次净化。
    """
    def strip_note(text: str) -> str:
        # 去掉含内部备注关键词的括号说明（全角/半角都处理）
        def repl(m: "re.Match") -> str:
            inner = m.group(2)
            return "" if any(k in inner for k in _NOTE_MARKERS) else m.group(0)
        return re.sub(r"([（(])([^（）()]*)([)）])", repl, text)

    for k, v in list(mapping.items()):
        if not isinstance(v, str) or k.startswith("_"):
            continue
        # 只清洗身份/联系/账户这类信息性字段。正文（事实与理由、诉讼请求、申请事项、
        # 情况说明、证据清单）里的“（见聊天记录第3页）”是正常表述，剥掉就变成残句。
        if not any(t in k for t in _SANITIZE_TARGETS):
            continue
        text = strip_note(v).strip()
        # 清洗后只剩占位文案或说明尾巴的，一律留空
        core = re.sub(r"[（(][^（）()]*[)）]", "", text).strip(" ，,。；;：:")
        if not core or any(t in core for t in ("见聊天记录", "待补充", "待确认", "待补", "待律师")):
            text = ""
        mapping[k] = text

    # 身份段落从 party_info 重建，避免沿用 findings 里的旧备注文字
    try:
        pinfo = pi.load(case_dir)
    except Exception:
        return
    for role, key in (("plaintiffs", "原告身份信息"), ("defendants", "被告身份信息")):
        parts = []
        for p in (pinfo.get(role) or []):
            seg = []
            if p.get("name"):
                seg.append(str(p["name"]))
            if p.get("sex"):
                seg.append(str(p["sex"]))
            if p.get("nation"):
                seg.append(f"{p['nation']}族")
            if p.get("birth"):
                seg.append(f"{p['birth']}出生")
            if p.get("address"):
                seg.append(f"住{str(p['address'])}")
            if p.get("id"):
                seg.append(f"身份证号：{p['id']}")
            if p.get("phone"):
                seg.append(f"联系电话：{p['phone']}")
            if seg:
                parts.append("，".join(seg))
        if parts:
            mapping[key] = "；".join(parts)


def _meta(fields: Dict[str, str]) -> Dict[str, Any]:
    return json.loads(fields.get("_meta") or "{}")


def _emit(template: Path, target_stem: Path, title: str, paras: List[str],
          fields: Dict[str, str], append_paras: bool = True) -> Dict[str, Any]:
    """按模板扩展名选择注入方式，返回实际落盘信息。

    硬规矩：模板只读，任何写入只发生在传进来的副本路径上。
    append_paras=False：模板已用占位符承载正文时不再追加「填写内容」段落，避免正文重复。
    """
    target_stem = target_stem.resolve()
    template = template.resolve()
    if template == target_stem or template.parent == target_stem.parent:
        raise SystemExit(f"拒绝写入模板目录，只能写副本：{target_stem}")

    if template.suffix.lower() == ".docx" and zipfile.is_zipfile(template):
        target = target_stem.with_suffix(".docx")
        shutil.copyfile(template, target)
        try:
            # 先把被告段的无角色占位符改成被告专用键，避免把原告信息填进被告栏
            qualify_role_placeholders(target)
            if append_paras:
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
           only: set[str] | None = None, skip_image_check: bool = False,
           extra: set[str] | None = None) -> Dict[str, Any]:
    case_dir = case_dir.resolve()
    # 图片体检放最前：先确保材料看全了，再谈确认和生成
    if not skip_image_check:
        try:
            import label_images
            img = label_images.collect(case_dir)
            if not img["ok"]:
                raise SystemExit(
                    f"图片材料未标注用途（{img['missing_count']}/{img['image_count']} 张）："
                    + "、".join(img["missing"][:6])
                    + "。必须先逐张看图并写入 05案件信息/image_labels.json，再生成文书。"
                )
        except ImportError:
            pass
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
    skipped: List[Dict[str, str]] = []

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
            # 多行正文、或不以「标签：」开头的段落，整段原样保留。
            # 否则证据清单这类纯文本会被当成「标签：值」解析，只留下含冒号的那一行。
            if "\n" in p.strip() or not _re.match(
                    r"^\s*[\u4e00-\u9fa5A-Za-z/（）()]{1,12}[：:]", p):
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

    def add(name: str, key: str, out_stem: str, title: str, paras: List[str],
            append_paras: bool = True, overrides: Dict[str, str] | None = None) -> None:
        if only is not None and key not in only:
            return
        try:
            entry = mt.ensure_template(root, key, registry)
            info = mt.resolve(key)
        except SystemExit:
            # 模板包缺文件：跳过这一份并记录，不能让整批文书都生成不出来
            skipped.append({"name": name, "key": key, "reason": "模板文件缺失"})
            return
        stem = (drafts / out_stem).resolve()
        if drafts.resolve() not in stem.parents:
            raise SystemExit(f"产物必须落在 04未签稿/，拒绝写入：{stem}")
        emitted = _emit(Path(entry["file"]), stem, title, keep(paras),
                        {**fields, **(overrides or {})}, append_paras=append_paras)
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
        merged = {**base, **{k: v for k, v in agent.items()
                             if isinstance(v, str) and not str(k).startswith("_")}}
        # 诉讼请求/案由/受理法院以规则与分析结果为准，禁止手写 JSON 覆盖：
        # 这三项是「每次出稿必须一致」的骨架，措辞由脚本按案情生成。
        for k in ("诉讼请求", "案由", "受理法院"):
            if str(base.get(k) or "").strip():
                merged[k] = base[k]
        # 无值/占位文案一律留空（含“（待确认，管辖由律师确认）”这类长串）
        blank_tokens = ("（待补充）", "(待补充)", "（待确认）", "(待确认)",
                        "待补充", "待确认", "（待补", "详见事实与理由")
        clean: Dict[str, str] = {}
        for k, v in merged.items():
            text = str(v or "").strip()
            if any(text == t or text.startswith(t) for t in blank_tokens):
                text = ""
            clean[k] = text
        return clean

    def add_filled(name: str, key: str, out_stem: str, title: str,
                   mapping: Dict[str, str] | None = None) -> None:
        """按模板占位符直接填值：保留模板全部原文与版式，不追加重复段落。"""
        if only is not None and key not in only:
            return
        try:
            entry = mt.ensure_template(root, key, registry)
            info = mt.resolve(key)
        except SystemExit:
            skipped.append({"name": name, "key": key, "reason": "模板文件缺失"})
            return
        template = Path(entry["file"])
        target = drafts / (out_stem + ".docx")
        if template.suffix.lower() == ".docx" and zipfile.is_zipfile(template):
            shutil.copyfile(template, target)
            fill_placeholders(target, mapping if mapping is not None else complaint_mapping(), blank_missing=True)
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

    def bank_account_mapping() -> Dict[str, str]:
        """当事人银行账户确认书占位符：全从 party_info 真实值取，无值留空。"""
        pl = (pi.load(case_dir).get("primary_plaintiff") or {})
        return {
            "案号": str((pi.load(case_dir).get("来源") or {}).get("案号") or ""),
            "户名": str(pl.get("account_name") or fields.get("户名") or ""),
            "收款账号": str(pl.get("account") or ""),
            "开户银行": str(pl.get("bank") or ""),
            "联行号": str(pl.get("raw", {}).get("联行号") or "") if isinstance(pl.get("raw"), dict) else "",
            "联系手机号码": str(pl.get("phone") or fields.get("联系方式") or ""),
            "邮编": str(pl.get("raw", {}).get("邮编") or "") if isinstance(pl.get("raw"), dict) else "",
        }

    def delivery_mapping() -> Dict[str, str]:
        """送达地址确认书占位符：填进表格单元格；地址/电话/账户从 party_info 取，无值留空。"""
        pl = (pi.load(case_dir).get("primary_plaintiff") or {})
        phone = str(pl.get("phone") or fields.get("联系方式") or "")
        return {
            "当事人": str(pl.get("name") or fields.get("当事人") or ""),
            "送达地址": str(pl.get("address") or ""),
            "联系电话": phone,
            "邮编": str(pl.get("raw", {}).get("邮编") or "") if isinstance(pl.get("raw"), dict) else "",
            "电子邮箱": str(pl.get("raw", {}).get("电子邮箱") or "") if isinstance(pl.get("raw"), dict) else "",
            "收款账号": str(pl.get("account") or ""),
            "开户银行": str(pl.get("bank") or ""),
            "户名": str(pl.get("account_name") or fields.get("户名") or ""),
            # 模板原文自带的句子占位：保证所提供的送达地址【真实有效】
            "地址": "真实有效",
        }

    def complaint_key() -> str:
        """诉状取模板：指定要素式且案由有对应模板才用要素式，否则用通用多当事人版。

        通用版按“一人一行”填，多被告各占一行，无值留空，多余行删除。
        """
        if fmt != "elements":
            return "complaint_general"
        picked = mt.complaint_template(str(meta.get("cause") or ""))
        return picked or "complaint_general"

    def complaint_row_mapping(pinfo: Dict[str, Any], m: Dict[str, str]) -> Dict[str, str]:
        """构造通用多当事人诉状的逐行占位符映射：原告N/被告N 的姓名/性别/民族/出生日期/住址/身份证号/电话。"""
        out: Dict[str, str] = {}
        fields_of = ("姓名", "性别", "民族", "出生日期", "住址", "身份证号", "电话")
        for prefix, people in (("原告", pinfo.get("plaintiffs") or []),
                               ("被告", pinfo.get("defendants") or [])):
            for i, p in enumerate(people, 1):
                src = {
                    "姓名": p.get("name", ""), "性别": p.get("sex", ""),
                    "民族": p.get("nation", ""), "出生日期": p.get("birth", ""),
                    "住址": p.get("address", ""), "身份证号": p.get("id", ""),
                    "电话": p.get("phone", ""),
                }
                for f in fields_of:
                    out[f"{prefix}{i}{f}"] = src[f]
        # 单值占位符
        out["案由"] = m.get("案由", "")
        out["诉讼请求"] = m.get("诉讼请求", "")
        out["事实与理由"] = m.get("事实与理由", "")
        out["受理法院"] = m.get("受理法院", "")
        return out

    if case_type == "zx":
        add("强制执行申请书", "enforcement", "01-强制执行申请书", "强制执行申请书", [
            f"申请执行人：{fields['申请执行人']}    被执行人：{fields['被执行人']}",
            f"## 执行依据\n{fields['执行依据']}",
            f"## 执行请求\n{fields['执行请求']}",
            f"## 事实与理由\n{fields['事实与理由']}",
            f"此致 {fields['受理法院']}", SIGN_LINE,
        ], append_paras=False)
        if not only_complaint:
            add("收款账户确认书", "payee_account", "06-收款账户确认书", "收款账户确认书", [
                f"申请执行人/领款人：{fields['申请执行人']}",
                f"开户行：{fields['开户行']}    账号：{fields['账号']}", SIGN_LINE,
            ])
    else:
        key = complaint_key()
        label = "要素式起诉状" if key.startswith("complaint_elements") else "起诉状"
        tpl_file = Path(mt.ensure_template(root, key, registry)["file"])
        complaint_stem = "01-起诉状"
        if key == "complaint_general" and zipfile.is_zipfile(tpl_file):
            # 通用多当事人版：按人数逐行填，多被告各占一行，无值留空，多余行删除
            if only is None or key in only:
                entry = mt.ensure_template(root, key, registry)
                info = mt.resolve(key)
                target = drafts / (complaint_stem + ".docx")
                shutil.copyfile(Path(entry["file"]), target)
                _pinfo = pi.load(case_dir)
                row_map = complaint_row_mapping(_pinfo, complaint_mapping())
                fill_complaint_rows(target, len(_pinfo.get("plaintiffs") or []),
                                    len(_pinfo.get("defendants") or []), row_map)
                docs.append({
                    "name": label, "file": f"04未签稿/{target.name}",
                    "slot": doc_slots.slot_name_for(label, case_type),
                    "slot_code": doc_slots.slot_code_for(label, case_type),
                    "template": f"{info['title']}（{info['src']}）",
                    "template_key": key, "template_status": info["status"],
                    "template_origin": str(info["path"]),
                    "inject_mode": "row_filled", "sign_positions": [SIGN_LINE],
                    "status": "generated",
                })
        elif zipfile.is_zipfile(tpl_file) and find_placeholders(tpl_file):
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
            # 一审用“当事人银行账户确认书”（表格版），不用退费确认书
            add_filled("账户确认书", "bank_account", "06-账户确认书", "当事人银行账户确认书",
                       bank_account_mapping())

    if not only_complaint:
        dpl = Path(mt.ensure_template(root, "delivery", registry)["file"])
        if find_placeholders(dpl):
            add_filled("送达地址确认书", "delivery", "05-送达地址确认书", "送达地址确认书",
                       delivery_mapping())
        else:
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
            ], append_paras=False)

    # 按需文书：财产保全/延期开庭/撤诉/追加被告/执行结案/情况说明等。
    # 正文一律由 `06运行记录/extra_docs.json` 里按 key 提供，脚本不编造申请事由。
    if extra:
        extra_src = _load(case_dir / "06运行记录" / "extra_docs.json")
        if not isinstance(extra_src, dict):
            extra_src = {}
        for key in extra:
            spec = EXTRA_DOCS.get(key)
            if not spec:
                skipped.append({"name": key, "key": key, "reason": "未登记该文书"})
                continue
            out_stem, title, append_paras = spec
            ov = extra_src.get(key) or {}
            ov = {k: v for k, v in ov.items()
                  if k in EXTRA_CONTENT_KEYS and isinstance(v, str) and v.strip()}
            # 申请事项：优先后台提供，其次按文书类型的固定程式生成；都没有就留空
            if not ov.get("申请事项") and EXTRA_DEFAULT_APPLY.get(key):
                try:
                    amt = _amount_display(str(_meta(fields).get("amount") or ""))
                except Exception:
                    amt = ""
                ov["申请事项"] = EXTRA_DEFAULT_APPLY[key].replace("{N}", amt or "相应")
            merged = {**fields, **ov}
            add(title, key, out_stem, title, [
                f"申请人：{merged.get('原告', '')}",
                f"## 申请事项\n{merged.get('申请事项', '')}",
                f"## 事实与理由\n{merged.get('事实与理由', '')}",
                f"此致 {merged.get('受理法院', '')}", SIGN_LINE,
            ], append_paras=append_paras, overrides=ov)

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
        slot = f"槽位 {doc['slot_code']} {doc['slot']}" if doc.get("slot_code") else "槽位待人工选择"
        hand = "　【一页式表格，正文需手工填写】" if doc.get("template_key") in EXTRA_HAND_FILL else ""
        lines.append(f"- {doc['name']}：`{doc['file']}`（{slot}）{hand}")
    guides = [d for d in docs if d.get("fill_guide")]
    if guides:
        lines += ["", "## 旧版 .doc 模板：需人工填写"]
        for doc in guides:
            lines.append(f"- `{doc['file']}` → 应填内容见 `{doc['fill_guide']}`")
    lines += ["", "## 需客户/律师补充 or 签字盖章"]
    lines += [f"- {item}" for item in client_items]
    # 材料提取件里标记为待核对的（如手写卡号易误读），强制提示与原件核对
    verify = pi.load(case_dir).get("待核对") or []
    for item in verify:
        lines.append(f"- 【务必核对原件】{item}")
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
        "skipped": skipped,
    }
    if skipped:
        lines = [f"- ⚠ 模板缺失未生成：{s['name']}（{s['key']}）" for s in skipped]
        checklist.write_text("\n".join(lines) + "\n\n" + checklist.read_text(encoding="utf-8"),
                             encoding="utf-8")
    out = case_dir / "05案件信息" / "document_manifest.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "manifest": str(out),
        "documents": [d["file"] for d in docs],
        "skipped": skipped,
        "checklist": f"04未签稿/{checklist.name}",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="生成整套可打印签字 Word")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--case-type", choices=["sp", "zx"], default="")
    ap.add_argument("--only-complaint", action="store_true", help="只生成诉状，不出整套材料")
    ap.add_argument("--only", default="", help="只生成指定模板 key，逗号分隔，如 delivery,account")
    ap.add_argument("--extra", default="",
                    help="按需文书 key，逗号分隔（保全/延期开庭/撤诉/追加被告/执行结案/情况说明等），"
                         "正文取自 06运行记录/extra_docs.json")
    ap.add_argument("--list-extra", action="store_true", help="列出全部可用的按需文书 key")
    ap.add_argument("--skip-image-check", action="store_true",
                    help="跳过图片标注体检（仅排障用；正常流程禁止跳过）")
    args = ap.parse_args()

    if args.list_extra:
        for k, (stem, title, _) in EXTRA_DOCS.items():
            print(f"{k:24s} {stem}  {title}")
        return 0

    case_dir = Path(args.case_dir).resolve()
    if not case_dir.exists():
        raise SystemExit(f"案件目录不存在：{case_dir}")
    case_type = args.case_type or str(_load(case_dir / "05案件信息" / "analysis_confirm.json").get("case_type") or "sp")
    only = {k.strip() for k in args.only.split(",") if k.strip()} if args.only else None
    extra = {k.strip() for k in args.extra.split(",") if k.strip()} if args.extra else None
    result = render(case_dir, case_type, only_complaint=args.only_complaint, only=only,
                    skip_image_check=args.skip_image_check, extra=extra)
    if args.only_complaint:
        result["documents"] = [d for d in result["documents"] if "起诉状" in d or "申请书" in d]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
