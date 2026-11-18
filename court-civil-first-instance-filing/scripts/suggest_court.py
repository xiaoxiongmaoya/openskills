#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推断受理法院：文书里没写「此致 XX 人民法院」时用。

    python suggest_court.py --case-dir "<案件目录>"
    python suggest_court.py --case-dir "<案件目录>" --region 510000 --json

规则（按优先级）：
  1. 文书明确写了法院 → 直接用
  2. 一般地域管辖：被告住所地（民诉法第 22 条）
  3. 合同纠纷：合同履行地。民间借贷以「接收货币一方所在地」为履行地 → 原告住所地
  4. 级别管辖：标的额决定基层/中级，超出基层上限要提示

输出候选法院 + 依据 + fyid。**最终由用户确认**，脚本不自动拍板。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from court_client import CourtClient, session_file_for  # noqa: E402
from read_office_text import clean, read_doc, read_docx  # noqa: E402

COURT_RE = re.compile(r"([\u4e00-\u9fa5]{2,20}?人民法院)")
TOKEN_RE = re.compile(r"([\u4e00-\u9fa5]{2,10}?(?:自治县|区|县|市|旗))")
# 基层法院一审标的额上限（地方口径，超过需报中院），仅作提示用
BASE_LIMIT = 50_000_000


def court_from_text(text: str) -> str:
    hits = [m.group(1) for m in COURT_RE.finditer(text)]
    if not hits:
        return ""
    # 取最后出现的（落款处），并去掉前缀"此致"
    return hits[-1].replace("此致", "").strip()


def region_tokens(address: str) -> List[str]:
    """从地址里取检索词，区县级优先于市级（"某某市"会命中一堆区法院）。"""
    raw = TOKEN_RE.findall(address or "")
    toks = []
    for t in raw:
        # 去掉「某省」「某某市」这种前缀，只留区县本体
        t = re.sub(r"^.*?[省市]", "", t).strip()
        if t and not t.endswith("省"):
            toks.append(t)
    district = [t for t in toks if t.endswith(("区", "县", "旗"))]
    city = [t for t in toks if t.endswith("市")]
    ordered: List[str] = []
    for t in district + city:
        if t not in ordered:
            ordered.append(t)
    return ordered[:2]


def load_text(case_dir: Path) -> str:
    drafts = case_dir / "04未签稿"
    for pattern in ("*.docx", "*.doc"):
        for f in sorted(drafts.glob(pattern)):
            if "起诉状" in f.name:
                raw = read_docx(f) if f.suffix.lower() == ".docx" else read_doc(f)
                return clean(raw)
    return ""


def suggest(case_dir: Path, region: str, client: CourtClient) -> Dict[str, Any]:
    spec = json.loads((case_dir / "05案件信息" / "spec.json").read_text(encoding="utf-8"))
    text = load_text(case_dir)
    explicit = court_from_text(text)
    amount = int(re.sub(r"\D", "", str(spec["case"].get("amount") or "0")) or 0)

    result: Dict[str, Any] = {
        "court_in_document": explicit,
        "amount": amount,
        "candidates": [],
        "needs_user_confirm": True,
        "note": "",
    }
    if explicit:
        # 诉状常省略省市前缀（如"某某区人民法院"），用区县名检索再按包含关系匹配
        base = explicit.replace("人民法院", "").strip()
        keyword = base[-4:] if len(base) > 4 else base
        found = client.search_courts(region, keyword) if region else []
        fyid, full_name = "", explicit
        for c in found:
            name = c.get("fymc") or c.get("mc") or ""
            if base and base in name and "中级" not in name:
                fyid = str(c.get("fyid") or c.get("id") or "")
                full_name = name
                break
        result["candidates"].append({
            "court": full_name, "fyid": fyid, "basis": "文书中明确写明", "priority": 1,
        })
        result["needs_user_confirm"] = not fyid
        result["note"] = "" if fyid else "文书中的法院名未匹配到平台记录，请手动确认"
        return result

    seen = set()
    for idx, (role, basis, priority) in enumerate((
        ("defendant", "被告住所地（一般地域管辖，民诉法第22条）", 1),
        ("plaintiff", "合同履行地：民间借贷以接收货币一方所在地（原告住所地）", 2),
    ), start=1):
        addr = (spec.get(role) or {}).get("address") or ""
        for token in region_tokens(addr):
            if not token or token in seen:
                continue
            seen.add(token)
            matched = False
            for c in client.search_courts(region, token)[:3]:
                name = c.get("fymc") or c.get("mc") or ""
                if "人民法院" not in name or "中级" in name or "高级" in name:
                    continue
                if any(x["court"] == name for x in result["candidates"]):
                    continue
                result["candidates"].append({
                    "court": name, "fyid": str(c.get("fyid") or c.get("id") or ""),
                    "basis": basis, "priority": priority, "matched_on": token,
                })
                matched = True
            if matched:
                break

    result["candidates"].sort(key=lambda x: x["priority"])
    if not result["candidates"]:
        result["note"] = "地址里没识别出区县名，也搜不到法院，需用户指定"
    elif amount > BASE_LIMIT:
        result["note"] = f"标的额 {amount} 超过基层法院一审上限，可能需中级法院，请人工确认级别管辖"
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="推断受理法院（要素式诉状未写法院时）")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--region", default="", help="省级代码，缺省用 spec.court.region")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    case_dir = Path(args.case_dir).resolve()
    spec = json.loads((case_dir / "05案件信息" / "spec.json").read_text(encoding="utf-8"))
    region = args.region or str(spec["court"].get("region") or "")

    client = CourtClient(base_url="https://zxfw.court.gov.cn",
                         session_file=str(session_file_for(case_dir)),
                         polite_delay=1.0)
    client.ensure_login()
    data = suggest(case_dir, region, client)

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(f"文书中的法院：{data['court_in_document'] or '未写明'}")
    print(f"标的额：{data['amount']}")
    for c in data["candidates"]:
        tag = "首选" if c["priority"] == 1 else "备选"
        print(f"  [{tag}] {c['court']} (fyid={c['fyid']}) —— {c['basis']}")
    if data["note"]:
        print(f"提示：{data['note']}")
    print("管辖属法律判断，落库前请用户确认。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
