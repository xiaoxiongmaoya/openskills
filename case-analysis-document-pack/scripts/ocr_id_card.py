#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""身份证截图 OCR 置信度判定：高置信度才写入当事人信息，低置信度留空并提示手填。

规则（本技能固化，避免每次人工判断）：
1. 优先用显式 OCR 引擎（pytesseract）识别；不可用时由 Agent 用视觉读图，
   把结果写入 05案件信息/id_ocr_input.json，本脚本负责判定与落库。
2. 置信度判定以“可验证性”为准，不靠主观：
   - 身份证号：18 位且校验位正确 = high；位数对但校验错 = low（留空）。
   - 姓名：2-4 个汉字且与已登记姓名一致 = high。
   - 性别/民族/出生日期：与身份证号可推导值一致 = high；冲突 = low。
   - 住址：含省/市/区县或长度≥6 = medium（可填但标待核）。
3. 低置信度字段一律不写入 party_info，留空，并在 _待核对 里写明“需手填”。
4. 不在技能目录、文档、测试写真实信息；只写案件目录 05案件信息/。

用法：
  python scripts/ocr_id_card.py --case-dir <案件目录> --apply
  python scripts/ocr_id_card.py --case-dir <案件目录> --json   # 只判定不落库

id_ocr_input.json 结构（Agent 视觉读图后写入）：
{
  "原告": {"姓名": "张三", "性别": "女", "民族": "汉", "出生日期": "1982-08-30",
           "住址": "四川省…", "身份证号": "5101…"},
  "被告": [{"姓名": "李四", "…": "…"}]
}
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STATE = "05案件信息"
_ID_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
_ID_CHECK = "10X98765432"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def id_checksum_ok(idno: str) -> bool:
    s = str(idno or "").strip().upper()
    if len(s) != 18 or not s[:17].isdigit():
        return False
    total = sum(int(s[i]) * _ID_WEIGHTS[i] for i in range(17))
    return _ID_CHECK[total % 11] == s[17]


def birth_from_id(idno: str) -> str:
    s = str(idno or "").strip()
    if len(s) != 18 or not s[:17].isdigit():
        return ""
    return f"{s[6:10]}-{s[10:12]}-{s[12:14]}"


def sex_from_id(idno: str) -> str:
    s = str(idno or "").strip()
    if len(s) != 18 or not s[:17].isdigit():
        return ""
    return "男" if int(s[16]) % 2 == 1 else "女"


def _norm_date(value: str) -> str:
    m = re.search(r"(\d{4})\D?(\d{1,2})\D?(\d{1,2})", str(value or ""))
    if not m:
        return ""
    return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def judge_one(rec: Dict[str, Any], known_name: str = "") -> Dict[str, Any]:
    """判定单个当事人的 OCR 置信度，返回 {字段: {值, 置信度}} 与低置信度待填清单。"""
    out: Dict[str, Dict[str, str]] = {}
    need_manual: List[str] = []

    idno = str(rec.get("身份证号") or "").strip().upper()
    if idno:
        if id_checksum_ok(idno):
            out["身份证号"] = {"值": idno, "置信度": "high"}
        else:
            out["身份证号"] = {"值": "", "置信度": "low"}
            need_manual.append("身份证号（校验位不通过，需手填核对）")
    else:
        out["身份证号"] = {"值": "", "置信度": "none"}
        need_manual.append("身份证号（未识别，需手填）")

    name = str(rec.get("姓名") or "").strip()
    if name and 2 <= len(name) <= 4 and all("\u4e00" <= ch <= "\u9fa5" for ch in name):
        if not known_name or name == known_name:
            out["姓名"] = {"值": name, "置信度": "high"}
        else:
            out["姓名"] = {"值": "", "置信度": "low"}
            need_manual.append("姓名（与登记不一致，需核对）")
    elif name:
        out["姓名"] = {"值": "", "置信度": "low"}
        need_manual.append("姓名（格式异常，需核对）")
    else:
        out["姓名"] = {"值": "", "置信度": "none"}
        need_manual.append("姓名（未识别，需手填）")

    # 性别/出生日期：与身份证号推导值交叉核验
    sex = str(rec.get("性别") or "").strip()
    derived_sex = sex_from_id(idno) if id_checksum_ok(idno) else ""
    if sex and (not derived_sex or sex == derived_sex):
        out["性别"] = {"值": sex, "置信度": "high" if derived_sex else "medium"}
    elif sex and derived_sex and sex != derived_sex:
        out["性别"] = {"值": "", "置信度": "low"}
        need_manual.append("性别（与身份证号推导不一致）")
    else:
        out["性别"] = {"值": derived_sex, "置信度": "high" if derived_sex else "none"}
        if not derived_sex:
            need_manual.append("性别（未识别，需手填）")

    birth = _norm_date(rec.get("出生日期") or "")
    derived_birth = birth_from_id(idno) if id_checksum_ok(idno) else ""
    if birth and (not derived_birth or birth == derived_birth):
        out["出生日期"] = {"值": birth, "置信度": "high" if derived_birth else "medium"}
    elif birth and derived_birth and birth != derived_birth:
        out["出生日期"] = {"值": "", "置信度": "low"}
        need_manual.append("出生日期（与身份证号推导不一致）")
    else:
        out["出生日期"] = {"值": derived_birth, "置信度": "high" if derived_birth else "none"}
        if not derived_birth:
            need_manual.append("出生日期（未识别，需手填）")

    # 民族：无法从身份证号推导，只认清晰识别（汉字）
    nation = str(rec.get("民族") or "").strip().rstrip("族")
    if nation and 1 <= len(nation) <= 4 and all("\u4e00" <= ch <= "\u9fa5" for ch in nation):
        out["民族"] = {"值": nation, "置信度": "medium"}
    else:
        out["民族"] = {"值": "", "置信度": "none"}
        need_manual.append("民族（未识别，需手填或留空）")

    # 住址：长度与行政区划特征判定
    addr = str(rec.get("住址") or rec.get("地址") or "").strip()
    if addr and len(addr) >= 6 and any(k in addr for k in ("省", "市", "区", "县", "镇", "乡", "村", "街", "路")):
        out["住址"] = {"值": addr, "置信度": "medium"}
    elif addr and len(addr) >= 10:
        out["住址"] = {"值": addr, "置信度": "medium"}
    else:
        out["住址"] = {"值": "", "置信度": "none"}
        need_manual.append("住址（未识别或过短，需手填）")

    return {"fields": out, "need_manual": need_manual}


def run(case_dir: Path, apply: bool) -> Dict[str, Any]:
    case_dir = case_dir.resolve()
    state = case_dir / STATE
    inp = _load(state / "id_ocr_input.json")
    if not isinstance(inp, dict):
        inp = {}
    party = _load(state / "party_info.json")

    # 已知姓名（用于交叉核验，不做 OCR 之外的新来源）
    known = {"原告": "", "被告": []}
    for p in (party.get("当事人") or []):
        role = str(p.get("角色") or "")
        if "被告" in role:
            known["被告"].append(str(p.get("姓名") or ""))
        else:
            if not known["原告"]:
                known["原告"] = str(p.get("姓名") or "")

    results: Dict[str, Any] = {}
    pl = inp.get("原告")
    if isinstance(pl, dict):
        results["原告"] = judge_one(pl, known["原告"])
    df_list = inp.get("被告")
    if isinstance(df_list, list) and df_list:
        results["被告"] = [judge_one(d, known["被告"][i] if i < len(known["被告"]) else "")
                           for i, d in enumerate(df_list)]

    payload = {"case_dir": str(case_dir), "judgements": results,
               "note": "high 可直接落库；medium 可落库但标待核；low/none 留空需手填"}

    if apply:
        # 只把 high/medium 且非空的字段写回 party_info；其余保持空
        def fill(target: Dict[str, Any], judged: Dict[str, Any]) -> None:
            for k, v in (judged.get("fields") or {}).items():
                val = str((v or {}).get("值") or "").strip()
                conf = str((v or {}).get("置信度") or "none")
                if val and conf in ("high", "medium") and not str(target.get(k) or "").strip():
                    target[k] = val

        # 按顺序匹配原告/被告列表
        pls = [p for p in (party.get("当事人") or []) if "被告" not in str(p.get("角色") or "")]
        dfs = [p for p in (party.get("当事人") or []) if "被告" in str(p.get("角色") or "")]
        if "原告" in results and pls:
            fill(pls[0], results["原告"])
        if "被告" in results:
            for i, judged in enumerate(results["被告"]):
                if i < len(dfs):
                    fill(dfs[i], judged)
        # 记录待手填
        manual: List[str] = []
        if "原告" in results:
            manual += [f"原告：{x}" for x in results["原告"].get("need_manual") or []]
        for i, judged in enumerate(results.get("被告") or []):
            nm = (dfs[i].get("姓名") if i < len(dfs) else "") or f"被告{i+1}"
            manual += [f"{nm}：{x}" for x in judged.get("need_manual") or []]
        party.setdefault("_待核对", [])
        for item in manual:
            if item not in party["_待核对"]:
                party["_待核对"].append(item)
        (state / "party_info.json").write_text(
            json.dumps(party, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["applied"] = True
        payload["manual_needed"] = manual

    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description="身份证 OCR 置信度判定与落库")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--apply", action="store_true", help="把高/中置信度字段写回 party_info")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    result = run(Path(args.case_dir), args.apply)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
