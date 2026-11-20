#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读解析老 Skill 的槽位常量，绝不 import 或运行其 main。"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Dict, Optional

HERE = Path(__file__).resolve().parent
OLD_SCRIPTS = HERE.parent.parent / "court-civil-first-instance-filing" / "scripts"
FILING_RUNNER = OLD_SCRIPTS / "filing_runner.py"
COURT_CLIENT = OLD_SCRIPTS / "court_client.py"


def _assignments(path: Path, names: set[str]) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"老 Skill 常量文件不存在：{path}")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: Dict[str, Any] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: Optional[ast.expr] = None
        if isinstance(node, ast.Assign):
            targets = node.targets
            value = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
            value = node.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id in names and value is not None:
                out[target.id] = ast.literal_eval(value)
    return out


_runner = _assignments(FILING_RUNNER, {"SLOT_ALIAS", "EXTRA_COPY"})
_client = _assignments(COURT_CLIENT, {"EXEC_SLOTS"})

SLOT_ALIAS: Dict[str, str] = dict(_runner.get("SLOT_ALIAS") or {})
EXTRA_COPY: Dict[str, str] = dict(_runner.get("EXTRA_COPY") or {})
EXEC_SLOTS: Dict[str, str] = dict(_client.get("EXEC_SLOTS") or {})

if not SLOT_ALIAS or not EXEC_SLOTS:
    raise SystemExit("未能从老 Skill 解析出 SLOT_ALIAS / EXEC_SLOTS，请检查版本")


def _first(*names: str) -> str:
    for name in names:
        if name in SLOT_ALIAS:
            return SLOT_ALIAS[name]
    raise SystemExit(f"SLOT_ALIAS 缺少槽位：{names}")


def slot_code_for(doc_name: str, case_type: str) -> str:
    name = str(doc_name)
    if case_type == "zx":
        if "强制执行申请书" in name:
            return _first("执行申请书", "起诉状")
        if "送达地址" in name:
            return _first("送达地址确认书")
        if "收款账户" in name or "账户确认书" in name:
            return _first("收款账户确认书", "账户确认书")
        if "证据目录" in name or "证据" in name:
            return _first("证据")
        if "授权委托" in name or "律师事务所函" in name or "律师" in name:
            return _first("授权委托书及代理人身份证明", "代理人手续")
        if "执行依据" in name:
            return _first("执行依据文书")
        if "身份" in name:
            return _first("申请人身份材料", "当事人身份证明")
    if "起诉状" in name:
        return _first("起诉状")
    if "送达地址" in name:
        return _first("送达地址确认书")
    if "账户确认书" in name:
        return _first("账户确认书", "收款账户确认书")
    if "证据目录" in name or "证据" in name:
        return _first("证据")
    if "授权委托" in name or "律师事务所函" in name or "律师" in name:
        return _first("代理人手续", "授权委托书及代理人身份证明")
    if "身份" in name:
        return _first("当事人身份证明", "申请人身份材料")
    raise SystemExit(f"未知文书类型，无法映射槽位：{name}")


def slot_name_for(doc_name: str, case_type: str) -> str:
    code = slot_code_for(doc_name, case_type)
    reverse = {v: k for k, v in SLOT_ALIAS.items()}
    if case_type == "zx":
        reverse.update(EXEC_SLOTS)
    else:
        # sp 一审：起诉状与执行申请书共用槽位码 11800016-2，
        # reverse 里“执行申请书”会覆盖“起诉状”。按文书类型纠正，避免清单显示成执行槽位。
        if "起诉状" in str(doc_name):
            reverse[code] = "起诉状"
    return reverse.get(code, code)


def extra_copy_for(doc_name: str) -> str:
    if "账户确认书" in doc_name:
        return EXTRA_COPY.get("账户确认书", "")
    return ""


if __name__ == "__main__":
    import json
    print(json.dumps({"SLOT_ALIAS": SLOT_ALIAS, "EXTRA_COPY": EXTRA_COPY, "EXEC_SLOTS": EXEC_SLOTS},
                     ensure_ascii=False, indent=2))
