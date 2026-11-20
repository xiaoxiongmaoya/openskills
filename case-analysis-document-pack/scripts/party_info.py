#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""当事人信息归一化：支持单人与多当事人。

`05案件信息/party_info.json` 有两种写法，本模块统一读成标准结构：

A. 单人（旧写法，兼容）
{
  "原告": "张三", "身份证号": "...", "电话": "...", "送达地址": "...",
  "收款户名": "...", "收款开户行": "...", "收款账号": "..."
}

B. 多当事人（新写法，推荐）
{
  "当事人": [
    {"角色": "原告", "姓名": "张三", "身份证号": "...", "电话": "...",
     "送达地址": "...", "收款户名": "...", "收款开户行": "...", "收款账号": "..."},
    {"角色": "原告", "姓名": "张三", ...},
    {"角色": "被告", "姓名": "李四", ...}
  ],
  "_来源": {...}, "_待核对": [...]
}

标准输出：
{
  "plaintiffs": [ {name, id, phone, address, account_name, bank, account, raw} ],
  "defendants": [...],
  "primary_plaintiff": {...} | {},
  "primary_defendant": {...} | {},
  "待核对": [...],
  "来源": {...}
}

多当事人时，模板里的“原告/被告”字段用顿号连接姓名，账户/送达只取第一个（签字确认书按人各出一份，
由 render 层决定是否拆分；此处先提供主当事人）。
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _fmt_nation(text: str) -> str:
    """民族统一成「汉族」这种带“族”的写法。

    模板里 {{民族}} 后面不带“族”字（带了民族为空会剩「，族」），
    所以规范值必须自带“族”；已带“族”的不重复加，空值保持空。
    """
    s = str(text or "").strip().strip("，,。；;")
    if not s:
        return ""
    return s if s.endswith("族") else s + "族"


def _fmt_birth(text: str) -> str:
    """把各种出生日期写法统一成诉状用的「YYYY年M月D日」。

    接受 1985-11-12 / 1985/11/12 / 19851112 / 1985年11月12日；
    年月日缺一不可，识别不出原样返回（由调用方决定是否留空）。
    """
    s = str(text or "").strip()
    if not s:
        return ""
    m = re.search(r"(\d{4})\s*[-/年.]\s*(\d{1,2})\s*[-/月.]\s*(\d{1,2})", s)
    if not m:
        m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if not m:
        return s
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return s
    return f"{y}年{mo}月{d}日"


def _birth_from_id(idno: str) -> str:
    """从 18 位身份证号第 7-14 位推导出生日期（YYYY年M月D日）；不可靠则返回空串。"""
    s = str(idno or "").strip()
    if len(s) != 18 or not s[:17].isdigit():
        return ""
    y, m, d = s[6:10], s[10:12], s[12:14]
    if not (y.isdigit() and m.isdigit() and d.isdigit()):
        return ""
    return _fmt_birth(f"{y}-{m}-{d}")


def _sex_from_id(idno: str) -> str:
    """从 18 位身份证号第 17 位奇偶推导性别（男/女）；不可靠则返回空串。"""
    s = str(idno or "").strip()
    if len(s) != 18 or not s[:17].isdigit():
        return ""
    return "男" if int(s[16]) % 2 == 1 else "女"


def _norm_one(item: Dict[str, Any], role: str) -> Dict[str, str]:
    idno = str(item.get("身份证号") or "").strip()
    # 性别/出生日期：优先显式给值，否则从身份证号推导；民族不能推导，只取显式值（无则留空）
    sex = str(item.get("性别") or "").strip() or _sex_from_id(idno)
    birth = _fmt_birth(str(item.get("出生日期") or "").strip()) or _birth_from_id(idno)
    return {
        "role": role,
        "name": str(item.get("姓名") or item.get("原告") or item.get("被告") or "").strip(),
        "id": idno,
        "sex": sex,
        "nation": _fmt_nation(str(item.get("民族") or "").strip()),
        "birth": birth,
        "phone": str(item.get("电话") or "").strip(),
        "address": str(item.get("送达地址") or item.get("住址") or "").strip(),
        "account_name": str(item.get("收款户名") or item.get("姓名") or "").strip(),
        "bank": str(item.get("收款开户行") or "").strip(),
        "account": str(item.get("收款账号") or "").strip(),
        "raw": item,
    }


def load(case_dir: Path) -> Dict[str, Any]:
    data = _load(Path(case_dir) / "05案件信息" / "party_info.json")
    if not isinstance(data, dict):
        data = {}

    plaintiffs: List[Dict[str, str]] = []
    defendants: List[Dict[str, str]] = []

    people = data.get("当事人")
    if isinstance(people, list) and people:
        for item in people:
            if not isinstance(item, dict):
                continue
            role = str(item.get("角色") or "").strip()
            if "被告" in role or "被执行" in role:
                defendants.append(_norm_one(item, "被告"))
            else:
                plaintiffs.append(_norm_one(item, "原告"))
    elif any(data.get(k) for k in ("原告", "身份证号", "电话", "送达地址", "收款账号")):
        # 旧单人写法
        pl = _norm_one({
            "姓名": data.get("原告"),
            "身份证号": data.get("身份证号"),
            "电话": data.get("电话"),
            "送达地址": data.get("送达地址"),
            "收款户名": data.get("收款户名"),
            "收款开户行": data.get("收款开户行"),
            "收款账号": data.get("收款账号"),
        }, "原告")
        if pl["name"] or pl["id"]:
            plaintiffs.append(pl)
        if data.get("被告"):
            defendants.append(_norm_one({"姓名": data.get("被告")}, "被告"))

    return {
        "plaintiffs": plaintiffs,
        "defendants": defendants,
        "primary_plaintiff": plaintiffs[0] if plaintiffs else {},
        "primary_defendant": defendants[0] if defendants else {},
        "待核对": data.get("_待核对") or [],
        "来源": data.get("_来源") or {},
        "multi": len(plaintiffs) > 1 or len(defendants) > 1,
    }


def join_names(people: List[Dict[str, str]], sep: str = "、") -> str:
    return sep.join(p["name"] for p in people if p.get("name"))
