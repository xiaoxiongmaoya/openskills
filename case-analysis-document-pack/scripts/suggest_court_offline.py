#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线管辖候选：只复用本地规则，绝不实例化 CourtClient、绝不登录法院。

老 Skill 的 suggest_court.py 在 L148-151 会触发法院登录，本脚本刻意不 import 它。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
OLD_SCRIPTS = HERE.parent.parent / "court-civil-first-instance-filing" / "scripts"
if OLD_SCRIPTS.exists():
    sys.path.insert(0, str(OLD_SCRIPTS))
from read_office_text import clean, read_doc, read_docx  # noqa: E402

COURT_RE = re.compile(r"([\u4e00-\u9fa5]{2,20}?人民法院)")
TOKEN_RE = re.compile(r"([\u4e00-\u9fa5]{2,10}?(?:自治县|区|县|市|旗))")
BASE_LIMIT = 50_000_000


def court_from_text(text: str) -> str:
    hits = [m.group(1) for m in COURT_RE.finditer(text or "")]
    if not hits:
        return ""
    return hits[-1].replace("此致", "").strip()


def region_tokens(address: str) -> List[str]:
    raw = TOKEN_RE.findall(address or "")
    toks: List[str] = []
    for token in raw:
        token = re.sub(r"^.*?[省市]", "", token).strip()
        if token and not token.endswith("省"):
            toks.append(token)
    district = [t for t in toks if t.endswith(("区", "县", "旗"))]
    city = [t for t in toks if t.endswith("市")]
    ordered: List[str] = []
    for token in district + city:
        if token not in ordered:
            ordered.append(token)
    return ordered[:2]


def load_document_text(case_dir: Path) -> str:
    drafts = case_dir / "04未签稿"
    for pattern in ("*.docx", "*.doc"):
        for path in sorted(drafts.glob(pattern)):
            if "起诉状" not in path.name:
                continue
            try:
                raw = read_docx(path) if path.suffix.lower() == ".docx" else read_doc(path)
                return clean(raw)
            except Exception:
                continue
    return ""


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def suggest(case_dir: Path, region: str = "") -> Dict[str, Any]:
    case_dir = case_dir.resolve()
    spec = _load(case_dir / "05案件信息" / "spec.json")
    text = load_document_text(case_dir)
    explicit = court_from_text(text)
    amount_raw = str((spec.get("case") or {}).get("amount") or "0")
    digits = re.sub(r"\D", "", amount_raw)
    amount = int(digits) if digits else 0

    result: Dict[str, Any] = {
        "court_in_document": explicit,
        "region": region or str((spec.get("court") or {}).get("region") or ""),
        "amount": amount,
        "candidates": [],
        "needs_user_confirm": True,
        "note": "",
        "fyid": "",
    }

    if explicit:
        result["candidates"].append({
            "court": explicit,
            "fyid": "",
            "basis": "文书中明确写明法院",
            "priority": 1,
            "needs_platform_check": True,
        })
        result["note"] = "法院名称来自文书；fyid 由后续立案阶段在法院平台核对。"
        return result

    seen: set[str] = set()
    roles = (
        ("defendant", "被告住所地（一般地域管辖，民诉法第22条）", 1),
        ("plaintiff", "合同履行地（民间借贷以接收货币一方所在地为履行地）", 2),
    )
    for role, basis, priority in roles:
        party = spec.get(role) or spec.get("plaintiffs" if role == "plaintiff" else "defendants") or {}
        if isinstance(party, list):
            party = party[0] if party else {}
        address = str(party.get("address") or "") if isinstance(party, dict) else ""
        for token in region_tokens(address):
            if not token or token in seen:
                continue
            seen.add(token)
            result["candidates"].append({
                "court": f"{token}人民法院（名称待平台核对）",
                "fyid": "",
                "basis": basis,
                "priority": priority,
                "matched_on": token,
            })

    if not result["candidates"]:
        result["note"] = "材料中未识别到法院或可检索的区县地址，需律师指定受理法院。"
    elif amount > BASE_LIMIT:
        result["note"] = (
            f"标的额 {amount:,} 元超过基层法院一审常见上限，可能需中级法院，请人工确认级别管辖。"
        )
    else:
        result["note"] = "候选仅为离线规则推断，需律师确认后再由立案阶段匹配 fyid。"
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="离线管辖候选（不登录法院）")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--region", default="")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    case_dir = Path(args.case_dir).resolve()
    if not case_dir.exists():
        raise SystemExit(f"案件目录不存在：{case_dir}")
    result = suggest(case_dir, args.region)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(f"文书中的法院：{result['court_in_document'] or '未写明'}")
    print(f"标的额：{result['amount']:,} 元")
    for item in result["candidates"]:
        tag = "首选" if item["priority"] == 1 else "备选"
        print(f"  [{tag}] {item['court']} —— {item['basis']}")
    if result["note"]:
        print(f"提示：{result['note']}")
    print("管辖属法律判断，需律师确认；本脚本未登录法院。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
