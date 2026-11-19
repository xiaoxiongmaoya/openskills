#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用模板库：从技能自带的实战模板包复制到 <工作空间根>/通用件/模板/ 并登记。

**硬规矩（不可违反）：模板不得修改，一律复制副本再使用。**
- 源 `assets/templates/` 只读，脚本永不写入；
- 副本落 `通用件/模板/` 后立即 chmod 444，Word/WPS 存不进去；
- 副本被改过 → `--verify` 报「副本被修改」，`--restore` 用源覆盖回原样；
- 所有生成文书都是 `04未签稿/` 里的副本，绝不在模板上原地填充。

设计变更（2026-11-19）：
- 删除“官网下载 + 生成骨架”那一套。模板只认 `assets/templates/`（律师实战模板包，
  已脱敏，随技能分发），缺文件直接报错，不再伪造任何模板。
- `assets/templates/_raw/` 保留原始模板包（含真实当事人信息，已 gitignore），
  仅在本机可用；仓库分发的只有脱敏版。
- 12 个 .doc 旧二进制格式无法无损脱敏，只在本机 `_raw` 命中时可用，
  登记状态标 `local_practice_raw_doc`，渲染时只复制原件、不注入内容。

用法：
  python scripts/manage_templates.py --list
  python scripts/manage_templates.py --root <工作空间根> --case-type sp
  python scripts/manage_templates.py --root <工作空间根> --need enforcement evidence --cause 民间借贷纠纷
  python scripts/manage_templates.py --root <工作空间根> --verify
  python scripts/manage_templates.py --root <工作空间根> --restore
  python scripts/manage_templates.py --root <工作空间根> --unlock   # 删除/迁移前解除只读
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

SKILL_DIR = Path(__file__).resolve().parent.parent
ASSETS = SKILL_DIR / "assets" / "templates"
RAW_ASSETS = ASSETS / "_raw"

SOURCE_LABEL = "本地实战模板包（律师实务，已脱敏）"

# key -> 登记项。file 为本地规范名（落 通用件/模板/），src 为模板包内的原始文件名。
TEMPLATES: Dict[str, Dict[str, Any]] = {
    # —— 起诉 / 答辩 ——
    "complaint_elements_loan": {
        "file": "02-要素式起诉状-民间借贷.docx",
        "src": "新借贷模板.docx",
        "title": "要素式民事起诉状（民间借贷纠纷）",
        "scope": ["sp"], "kind": "complaint", "causes": ["民间借贷纠纷", "借款合同纠纷"],
    },
    "complaint_elements_traffic": {
        "file": "02-要素式起诉状-机动车交通事故.docx",
        "src": "机动车交通事故责任纠纷起诉状.docx",
        "title": "要素式民事起诉状（机动车交通事故责任纠纷）",
        "scope": ["sp"], "kind": "complaint", "causes": ["机动车交通事故责任纠纷"],
    },
    "complaint_elements_sale": {
        "file": "02-要素式起诉状-买卖合同.doc",
        "src": "新买卖要素式.doc",
        "title": "要素式民事起诉状（买卖合同纠纷）",
        "scope": ["sp"], "kind": "complaint", "causes": ["买卖合同纠纷", "买卖合同"],
    },
    "answer_construction": {
        "file": "10-答辩状-建设工程施工合同.docx",
        "src": "建设工程施工合同纠纷答辩状.docx",
        "title": "民事答辩状（建设工程施工合同纠纷）",
        "scope": ["sp"], "kind": "optional",
    },
    # —— 立案通用件 ——
    "delivery": {
        # 通用占位符版：字段填进表格对应单元格，不再往文末追加“填写内容”
        "file": "03-送达地址确认书.docx",
        "src": "送达地址确认书-通用版.docx",
        "title": "人民法院案件送达地址确认书",
        "scope": ["sp", "zx"], "kind": "core",
    },
    "delivery_court_2025_doc": {
        "file": "03-送达地址确认书-法院版.doc",
        "src": "送达地址确认书（最新）.doc",
        "title": "送达地址确认书（法院最新版）",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "account": {
        "file": "04-账户确认书.docx",
        "src": "诉讼费退费及收取生效判决金钱债权账户确认书.docx",
        "title": "诉讼费退费及收取金钱债权账户确认书",
        "scope": ["sp", "zx"], "kind": "core",
    },
    "bank_account": {
        # 一审账户确认书：当事人银行账户确认书（通用占位符版，保留表格原文与签字栏）
        "file": "04-当事人银行账户确认书.docx",
        "src": "当事人银行账户确认书-通用版.docx",
        "title": "当事人银行账户确认书",
        "scope": ["sp", "zx"], "kind": "core",
    },
    "payee_account": {
        "file": "04-收款账户确认书.doc",
        "src": "当事人银行账户确认书（新版）.doc",
        "title": "当事人银行账户确认书（旧版 .doc，仅人工填写）",
        "scope": ["zx"], "kind": "core",
    },
    "evidence": {
        "file": "05-证据目录.docx",
        "src": "证据目录.docx",
        "title": "证据目录",
        "scope": ["sp", "zx"], "kind": "core",
    },
    "evidence_detail": {
        "file": "05-证据目录及说明.docx",
        "src": "证据目录及说明（适用于各类案件）.docx",
        "title": "证据目录及说明",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "evidence_detailed": {
        "file": "05-证据材料清单-详细.docx",
        "src": "（详细）证据清单.docx",
        "title": "诉讼当事人提交证据材料清单",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "evidence_catalog_doc": {
        "file": "05-证据目录-旧版.doc",
        "src": "证据目录模板.doc",
        "title": "证据目录（旧版）",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "authorization": {
        "file": "06-授权委托书.docx",
        "src": "法山立案授权委托书.docx",
        "title": "授权委托书（立案用）",
        "scope": ["sp", "zx"], "kind": "core",
    },
    "authorization_company": {
        "file": "06-授权委托书-单位版.docx",
        "src": "授权委托书 - 副本.docx",
        "title": "授权委托书（单位委托）",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "authorization_relative_doc": {
        "file": "06-授权委托书-亲属版.doc",
        "src": "亲属授权委托书模板.doc",
        "title": "授权委托书（亲属代理）",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "legal_rep": {
        "file": "06-法定代表人身份证明书.docx",
        "src": "法定代表人身份证明书1.docx",
        "title": "法定代表人身份证明书",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "material_list_doc": {
        "file": "11-提交材料清单.doc",
        "src": "材料清单 (1).doc",
        "title": "立案材料清单",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    # —— 执行 ——
    "enforcement": {
        "file": "07-强制执行申请书.docx",
        "src": "强制执行申请书.docx",
        "title": "强制执行申请书",
        "scope": ["zx"], "kind": "core",
    },
    "enforcement_elements": {
        "file": "07-强制执行申请书-要素式.docx",
        "src": "要素式执行.docx",
        "title": "强制执行申请书（申请执行用·要素式）",
        "scope": ["zx"], "kind": "optional",
    },
    "enforcement_close": {
        "file": "12-执行结案申请书.docx",
        "src": "执行结案申请书.docx",
        "title": "执行结案申请书",
        "scope": ["zx"], "kind": "optional",
    },
    "net_inquiry_doc": {
        "file": "13-网络查控申请书.doc",
        "src": "网络查控申请书.doc",
        "title": "网络查控申请书",
        "scope": ["zx"], "kind": "optional",
    },
    # —— 保全 ——
    "preservation": {
        "file": "14-财产保全申请书.docx",
        "src": "财产保全申请书.docx",
        "title": "财产保全申请书",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "preservation_withdraw": {
        "file": "15-撤回财产保全申请书.docx",
        "src": "撤回财产保全申请书.docx",
        "title": "撤回财产保全申请书",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "preservation_clues": {
        "file": "16-财产保全被告财产线索清单.docx",
        "src": "财产保全被告财产线索清单.docx",
        "title": "财产保全被告财产线索清单",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "property_clues": {
        "file": "16-财产线索清单.docx",
        "src": "财产线索(1).docx",
        "title": "财产线索",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    # —— 程序性申请 ——
    "mediation": {
        "file": "17-先行调解申请书.docx",
        "src": "先行调解申请书.docx",
        "title": "选择诉前（诉中）先行调解申请书",
        "scope": ["sp"], "kind": "optional",
    },
    "postpone_hearing": {
        "file": "18-延期开庭申请书.docx",
        "src": "延期开庭申请书.docx",
        "title": "延期开庭申请书",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "online_hearing": {
        "file": "19-线上开庭申请书.docx",
        "src": "线上开庭申请书(1).docx",
        "title": "线上开庭申请书",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "online_hearing_alt": {
        "file": "19-线上开庭申请书-简版.docx",
        "src": "在线开庭书.docx",
        "title": "线上开庭申请书（简版）",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "withdraw_suit_doc": {
        "file": "20-撤诉申请书.doc",
        "src": "撤诉申请书.doc",
        "title": "撤诉申请书",
        "scope": ["sp"], "kind": "optional",
    },
    "add_defendant_doc": {
        "file": "21-追加被告申请书.doc",
        "src": "追加被告申请书.doc",
        "title": "追加被告申请书",
        "scope": ["sp"], "kind": "optional",
    },
    # —— 情况说明 ——
    "statement_general": {
        "file": "22-情况说明.docx",
        "src": "情况说明模版.docx",
        "title": "情况说明",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "statement_filing": {
        "file": "22-立案情况说明.docx",
        "src": "立案情况说明.docx",
        "title": "立案情况说明",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "statement_filing_other": {
        "file": "22-立案情况说明-补充材料.docx",
        "src": "其他材料-立案情况说明.docx",
        "title": "立案情况说明（补充材料用）",
        "scope": ["sp", "zx"], "kind": "optional",
    },
    "statement_cash": {
        "file": "22-现金交付情况说明.docx",
        "src": "现金情况说明.docx",
        "title": "现金交付情况说明",
        "scope": ["sp"], "kind": "optional",
    },
    # —— 专项 ——
    "debt_transfer": {
        "file": "23-债权转移确认书.docx",
        "src": "债权转移确认书（劳务合同纠纷专用）.docx",
        "title": "债权转移确认书（劳务合同纠纷）",
        "scope": ["sp"], "kind": "optional",
    },
    "labor_entrust": {
        "file": "24-劳务欠款纠纷诉讼委托书.docx",
        "src": "劳务欠款纠纷诉讼委托书.docx",
        "title": "劳务欠款纠纷诉讼委托书",
        "scope": ["sp"], "kind": "optional",
    },
    "arbitration": {
        "file": "30-仲裁申请书.docx",
        "src": "1、仲裁申请书.docx",
        "title": "仲裁申请书",
        "scope": ["sp"], "kind": "optional",
    },
    "arbitration_delivery": {
        "file": "30-仲裁当事人送达信息确认书.docx",
        "src": "6、当事人送达信息确认书.docx",
        "title": "当事人送达信息确认书（仲裁立案）",
        "scope": ["sp"], "kind": "optional",
    },
    "evidence_list_arb": {
        "file": "30-申请人证据材料清单.docx",
        "src": "5、申请人证据材料清单.docx",
        "title": "申请人证据材料清单（仲裁）",
        "scope": ["sp"], "kind": "optional",
    },
    "answer_arb_doc": {
        "file": "30-答辩书.doc",
        "src": "7、答辩书.doc",
        "title": "答辩书（仲裁）",
        "scope": ["sp"], "kind": "optional",
    },
    "arbitration_doc": {
        "file": "30-仲裁模板.doc",
        "src": "仲裁模板.doc",
        "title": "仲裁申请书（旧版）",
        "scope": ["sp"], "kind": "optional",
    },
}

DEFAULT_NEEDS = {
    "sp": ["delivery", "account", "evidence", "authorization"],
    "zx": ["enforcement", "delivery", "payee_account", "evidence", "authorization"],
}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


READONLY_MODE = 0o444
WRITABLE_MODE = 0o666


def _lock(path: Path) -> None:
    """模板文件设为只读：物理上禁止原地修改。"""
    try:
        path.chmod(READONLY_MODE)
    except OSError:
        pass


def _unlock(path: Path) -> None:
    try:
        path.chmod(WRITABLE_MODE)
    except OSError:
        pass


def _is_readonly(path: Path) -> bool:
    return not bool(path.stat().st_mode & 0o200)


def _registry_path(template_dir: Path) -> Path:
    return template_dir / "来源登记.json"


def _load_registry(template_dir: Path) -> Dict[str, Any]:
    path = _registry_path(template_dir)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("templates"), dict):
                return data
        except Exception:
            pass
    return {"updated_at": "", "source": SOURCE_LABEL, "templates": {}}


def _save_registry(template_dir: Path, registry: Dict[str, Any]) -> None:
    registry["updated_at"] = datetime.now().isoformat(timespec="seconds")
    registry["source"] = SOURCE_LABEL
    _registry_path(template_dir).write_text(
        json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def source_path(src: str) -> Optional[Path]:
    """先找脱敏版，再找本机原始包（.doc 只存在于原始包）。"""
    for base in (ASSETS, RAW_ASSETS):
        candidate = base / src
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
    return None


def resolve(key: str) -> Dict[str, Any]:
    """解析模板 key，返回源文件与状态；缺失抛错，绝不生成替代文件。"""
    if key not in TEMPLATES:
        raise SystemExit(f"未知模板 key：{key}；可用：{', '.join(sorted(TEMPLATES))}")
    tpl = TEMPLATES[key]
    path = source_path(tpl["src"])
    if path is None:
        raise SystemExit(
            f"模板缺失：{tpl['src']}（key={key}）。"
            f"请把实战模板包放到 {ASSETS} 或 {RAW_ASSETS}，本技能不再生成替代模板。"
        )
    sanitized = path.parent == ASSETS
    return {
        "key": key,
        "title": tpl["title"],
        "file": tpl["file"],
        "src": tpl["src"],
        "path": path,
        "status": "local_practice" if sanitized else "local_practice_raw_doc",
        "extension": path.suffix.lower(),
    }


def complaint_template(cause: str) -> Optional[str]:
    """按案由挑要素式起诉状；无匹配返回 None（由调用方走普通版）。"""
    cause = (cause or "").strip()
    best = None
    for key, tpl in TEMPLATES.items():
        if tpl.get("kind") != "complaint":
            continue
        for c in tpl.get("causes", []):
            if cause and (c in cause or cause in c):
                return key
            if best is None:
                best = key
    return None if cause else best


def ensure_template(root: Path, key: str, registry: Dict[str, Any]) -> Dict[str, Any]:
    """把模板复制到通用件/模板/ 并设为只读。

    硬规矩：模板不得修改。这里做三件事——
    1. 只在副本上落盘，源文件（assets/templates）永不写；
    2. 复制后 chmod 444，Word/WPS 无法原地保存；
    3. 发现副本 hash 跟源不一致（被改过）→ 删掉重拷，登记 restored=true。
    """
    info = resolve(key)
    template_dir = root / "通用件" / "模板"
    template_dir.mkdir(parents=True, exist_ok=True)
    target = template_dir / info["file"]
    src = info["path"]
    src_hash = _sha256(src)
    restored = False

    if target.exists():
        if _sha256(target) == src_hash:
            _lock(target)
            status = registry.get("templates", {}).get(info["file"], {}).get("status", info["status"])
            return {
                "key": key, "file": str(target), "status": status,
                "reused": True, "restored": False, "readonly": _is_readonly(target),
            }
        _unlock(target)
        target.unlink()
        restored = True

    shutil.copyfile(src, target)
    _lock(target)
    registry.setdefault("templates", {})[info["file"]] = {
        "key": key,
        "title": info["title"],
        "source": SOURCE_LABEL,
        "origin_file": info["src"],
        "status": info["status"],
        "sha256": src_hash,
        "bytes": target.stat().st_size,
        "copied_at": datetime.now().isoformat(timespec="seconds"),
        "pii_masked": info["status"] == "local_practice",
        "readonly": True,
    }
    return {
        "key": key, "file": str(target), "status": info["status"],
        "reused": restored, "restored": restored, "readonly": _is_readonly(target),
    }


def verify(root: Path, restore: bool = False) -> Dict[str, Any]:
    """校验通用件/模板 下每个副本是否与 assets 源一致、是否只读。"""
    template_dir = root / "通用件" / "模板"
    registry = _load_registry(template_dir)
    problems: List[Dict[str, Any]] = []
    fixed: List[Dict[str, Any]] = []
    for file_name, entry in (registry.get("templates") or {}).items():
        key = str(entry.get("key") or "")
        target = template_dir / file_name
        if not key or key not in TEMPLATES:
            problems.append({"file": file_name, "issue": "登记里 key 无效", "key": key})
            continue
        src = source_path(TEMPLATES[key]["src"])
        if src is None:
            problems.append({"file": file_name, "issue": "源模板缺失", "key": key})
            continue
        if not target.exists():
            problems.append({"file": file_name, "issue": "副本缺失", "key": key})
            continue
        if _sha256(target) != _sha256(src):
            problems.append({"file": file_name, "issue": "副本被修改", "key": key})
            if restore:
                ensure_template(root, key, registry)
                problems[-1]["restored"] = True
            continue
        if not _is_readonly(target):
            _lock(target)
            fixed.append({"file": file_name, "issue": "已补设为只读", "key": key})
    if restore:
        _save_registry(template_dir, registry)
    return {
        "ok": not problems,
        "template_dir": str(template_dir),
        "checked": len(registry.get("templates") or {}),
        "problems": problems,
        "fixed": fixed,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="复制并登记本地实战模板")
    ap.add_argument("--root", help="工作空间根（不传则只解析校验）")
    ap.add_argument("--case-type", choices=["sp", "zx"], default="sp")
    ap.add_argument("--need", nargs="*", default=[])
    ap.add_argument("--cause", default="", help="用于挑要素式起诉状")
    ap.add_argument("--list", action="store_true", help="列出模板登记表")
    ap.add_argument("--verify", action="store_true", help="校验副本是否被改过、是否只读")
    ap.add_argument("--restore", action="store_true", help="把被改过的副本恢复成源模板")
    ap.add_argument("--unlock", action="store_true", help="解除副本只读（仅用于删除/迁移前）")
    args = ap.parse_args()

    if args.unlock:
        if not args.root:
            raise SystemExit("--unlock 需要 --root")
        template_dir = Path(args.root).resolve() / "通用件" / "模板"
        count = 0
        for path in template_dir.glob("*"):
            if path.is_file() and path.name != "来源登记.json":
                _unlock(path)
                count += 1
        print(json.dumps({"ok": True, "unlocked": count, "dir": str(template_dir)},
                         ensure_ascii=False, indent=2))
        return 0

    if args.list:
        rows = [
            {"key": k, "file": v["file"], "src": v["src"], "title": v["title"],
             "scope": v["scope"], "kind": v["kind"]}
            for k, v in TEMPLATES.items()
        ]
        print(json.dumps({"count": len(rows), "templates": rows}, ensure_ascii=False, indent=2))
        return 0

    if args.verify or args.restore:
        if not args.root:
            raise SystemExit("--verify/--restore 需要 --root")
        res = verify(Path(args.root).resolve(), restore=args.restore)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] or args.restore else 1

    needs: List[str] = list(args.need or DEFAULT_NEEDS[args.case_type])
    if args.case_type == "sp":
        picked = complaint_template(args.cause) if args.cause else None
        if picked and picked not in needs:
            needs.insert(0, picked)

    if not args.root:
        print(json.dumps({"ok": True, "resolved": [resolve(k)["status"] for k in needs],
                          "keys": needs}, ensure_ascii=False))
        return 0

    root = Path(args.root).resolve()
    if not root.exists():
        raise SystemExit(f"工作空间根不存在：{root}")
    template_dir = root / "通用件" / "模板"
    template_dir.mkdir(parents=True, exist_ok=True)
    registry = _load_registry(template_dir)
    results = [ensure_template(root, key, registry) for key in needs]
    _save_registry(template_dir, registry)
    print(json.dumps({
        "ok": True,
        "template_dir": str(template_dir),
        "registry": str(_registry_path(template_dir)),
        "templates": results,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
