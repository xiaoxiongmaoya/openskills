#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线材料门禁：**不登录、不建草稿**，只检查本地材料是否够格去立案。

为什么要有这一步：`organize_materials.py` 只能按扩展名/关键词归类，判不了
“这份 PDF 到底签没签字”“律师证是不是漏了”。以前是先建草稿再发现材料不对，
结果要么重建单、要么重传一轮（一案 80+ 次请求白跑）。

用法：
    python preflight_materials.py --case-dir "<案件目录>"
    python preflight_materials.py --case-dir "<案件目录>" --json
    python preflight_materials.py --case-dir "<案件目录>" --allow-missing-source

退出码：0 = 可以建档；1 = 有硬错误，禁止建档。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

# 法院材料槽：规范名 → 槽位键（与 filing_runner.SLOT_ALIAS 对齐）
SLOT_KEYS = {
    "起诉状": "起诉状",
    "当事人身份证明": "当事人身份证明",
    "代理人手续": "代理人手续",
    "证据": "证据",
    "送达地址确认书": "送达地址确认书",
    "账户确认书": "账户确认书",
    "其他材料": "其他材料",
    # 执行案件
    "执行申请书": "执行申请书",
    "执行依据文书": "执行依据文书",
    "申请人身份材料": "申请人身份材料",
    "授权委托书及代理人身份证明": "授权委托书及代理人身份证明",
    "收款账户确认书": "收款账户确认书",
}
REQUIRED_SLOTS = ("起诉状", "当事人身份证明", "证据", "送达地址确认书")
# 执行立案必传：执行申请书 + 执行依据文书 + 申请人身份材料
EXEC_REQUIRED_SLOTS = ("执行申请书", "执行依据文书", "申请人身份材料")
ALLOWED_EXT = {".pdf", ".jpg", ".jpeg", ".png"}
SOURCE_EXT = {".doc", ".docx", ".wps"}


def _load(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _resolve(raw: str, root: Path) -> Path:
    """spec 里的路径可能是相对路径（历史案件写成 `案件库\\...`），按工作空间根解析。"""
    path = Path(raw)
    if path.is_absolute():
        return path
    for base in (root, Path.cwd()):
        candidate = (base / path)
        if candidate.exists():
            return candidate
    return root / path


def check(case_dir: Path, allow_missing_source: bool = False,
          case_type: str = "") -> Dict[str, Any]:
    case_dir = case_dir.resolve()
    spec_path = case_dir / "05案件信息" / "spec.json"
    errors: List[str] = []
    warnings: List[str] = []
    info: List[str] = []

    if not spec_path.exists():
        return {"ok": False, "errors": [f"缺少 spec.json：{spec_path}"], "warnings": [], "info": []}
    try:
        spec = _load(spec_path)
    except (OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "errors": [f"spec.json 解析失败：{exc}"], "warnings": [], "info": []}

    case_type = case_type or str(spec.get("case_type") or "sp")
    is_zx = case_type == "zx"

    for key in ("court", "case", "materials", "claims"):
        if key not in spec:
            errors.append(f"spec 缺少必填段：{key}")
    if not (spec.get("plaintiffs") or spec.get("plaintiff")):
        errors.append("spec 缺少申请执行人" if is_zx else "spec 缺少原告（plaintiffs 列表 或 单数 plaintiff）")
    if not (spec.get("defendants") or spec.get("defendant")):
        errors.append("spec 缺少被执行人" if is_zx else "spec 缺少被告（defendants 列表 或 单数 defendant）")
    claims = spec.get("claims") or {}
    for key in ("ssqq", "ssly"):
        if not str(claims.get(key) or "").strip():
            label = "执行请求/执行理由" if is_zx else "诉讼请求/事实与理由"
            errors.append(f"claims.{key} 为空（{label}不能空）")
    if is_zx:
        zxyj = spec.get("zxyj") or {}
        if not str(zxyj.get("ah") or "").strip():
            errors.append("zxyj.ah 为空（执行依据文书文号必填）")
        if not str(zxyj.get("ysajbh") or "").strip():
            errors.append("zxyj.ysajbh 为空（原审案件 id 必填，可用 list_ysaj 查）")
    elif not str((spec.get("case") or {}).get("amount") or "").strip():
        errors.append("case.amount 为空")

    # ---- 槽位与文件 ----
    root = case_dir.parent.parent if case_dir.parent.name == "案件库" else case_dir
    slots_present: Dict[str, List[str]] = {}
    slots_declared: Dict[str, int] = {}
    for item in spec.get("materials") or []:
        slot = str(item.get("slot") or "")
        files = item.get("files") or []
        if not slot:
            errors.append("materials 有空槽位名")
            continue
        slots_present.setdefault(slot, [])
        slots_declared[slot] = slots_declared.get(slot, 0) + len(files)
        for raw in files:
            path = _resolve(str(raw), root)
            if not path.exists():
                errors.append(f"[{slot}] 文件不存在：{raw}")
                continue
            slots_present[slot].append(path.name)
            if path.suffix.lower() not in ALLOWED_EXT:
                errors.append(f"[{slot}] 法院不收的格式 {path.suffix}：{path.name}（只收 pdf/jpg/png）")
            if not path.name[:2].isdigit():
                warnings.append(f"[{slot}] 文件名没有两位序号前缀：{path.name}")

    required = EXEC_REQUIRED_SLOTS if is_zx else REQUIRED_SLOTS
    for slot in required:
        if not slots_declared.get(slot):
            errors.append(f"必传槽位为空：{slot}")

    # ---- 代理律师必须带执业证（执行案件本人申请时不需要代理人）----
    agent = spec.get("agent") or {}
    agent_on = agent.get("enabled", not is_zx)
    if agent_on:
        agent_slot = "授权委托书及代理人身份证明" if is_zx else "代理人手续"
        names = slots_present.get(agent_slot, [])
        if not any(("律师证" in n or "执业证" in n) for n in names):
            errors.append(f"「{agent_slot}」缺少律师执业证（法院实务必传，模板写非必传是坑）")
        if not any("授权委托书" in n or "委托书" in n for n in names):
            errors.append(
                f"「{agent_slot}」缺少授权委托书（PDF 或签字扫描 jpg 都算，但必须进这个槽；"
                "常见坑：签字件被归到 03图片件 后漏加进 spec 的代理人手续 files）"
            )
        if not any("所函" in n or "律师函" in n for n in names):
            errors.append(
                f"「{agent_slot}」缺少律师事务所函（所函须律所盖章件 PDF/jpg；"
                "客户只给未盖章 .doc 时不能跳过，必须先补盖章扫描件）"
            )

    # ---- 信息源优先级：Word 原稿 > 判决书 PDF 文本层 > 扫描件（两类案件统一）----
    drafts = [p for p in (case_dir / "04未签稿").glob("*") if p.suffix.lower() in SOURCE_EXT]
    if not drafts and not allow_missing_source:
        if is_zx:
            # 执行案件的必备材料本身就是判决书 PDF / 签字扫描件，客户常常不给 Word；
            # 不阻断，但必须显式标注「非 Word 来源，逐项人工核对」。
            warnings.append(
                "04未签稿 无 Word 原稿：当事人/案号/金额将按判决书 PDF 或扫描件读取，"
                "提交前须逐项人工核对（Word 优先，能要到原稿就要）"
            )
        else:
            errors.append("04未签稿 里没有 Word 原稿：当事人/金额/证件号必须从 Word 取，不能解析扫描 PDF")

    # ---- 原始包是否还有没归位的文件 ----
    raw_dir = case_dir / "01原始包" / "解压"
    if raw_dir.exists():
        placed = {p.name for p in (case_dir / "02签字件").glob("*")}
        placed |= {p.name for p in (case_dir / "03图片件").glob("*")}
        placed |= {p.name for p in (case_dir / "04未签稿").glob("*")}
        leftovers = [
            p for p in raw_dir.rglob("*")
            if p.is_file() and p.name not in placed and not p.name.startswith(".")
        ]
        if leftovers:
            warnings.append(
                f"01原始包/解压 有 {len(leftovers)} 个文件没归位（可能是无关件，也可能是漏分类）："
                + "、".join(p.name for p in leftovers[:6])
                + ("…" if len(leftovers) > 6 else "")
            )

    # ---- 签字件需要人眼确认（脚本无法判断）----
    signed = sorted((case_dir / "02签字件").glob("*.pdf"))
    if signed:
        info.append("请人眼扫一眼这些签字件（脚本判不了签章）：" + "、".join(p.name for p in signed))

    return {
        "ok": not errors,
        "case_dir": str(case_dir),
        "errors": errors,
        "warnings": warnings,
        "info": info,
        "slots": {k: v for k, v in slots_present.items()},
        "counts": {"files": sum(len(v) for v in slots_present.values()), "slots": len(slots_present)},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="离线材料门禁（不联网、不建档）")
    ap.add_argument("--case-dir", required=True)
    ap.add_argument("--json", action="store_true", help="只输出 JSON")
    ap.add_argument("--allow-missing-source", action="store_true", help="允许没有 Word 原稿（不推荐）")
    ap.add_argument("--case-type", choices=["sp", "zx"], default="",
                    help="sp=民事一审（默认）/ zx=强制执行申请；不填读 spec.case_type")
    args = ap.parse_args()

    result = check(Path(args.case_dir), args.allow_missing_source, case_type=args.case_type)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"材料门禁：{'通过' if result['ok'] else '未通过'}  "
              f"（{result.get('counts', {}).get('files', 0)} 份 / {result.get('counts', {}).get('slots', 0)} 槽）")
        for item in result["errors"]:
            print(f"  [!!] {item}")
        for item in result["warnings"]:
            print(f"  [警告] {item}")
        for item in result["info"]:
            print(f"  [核对] {item}")
        if result["ok"]:
            print("  可以建档。")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
