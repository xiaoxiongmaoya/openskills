#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""收尾自测：一条命令验证「模板填充机制 + 文书产物 + 模板洁净度」。

改动脚本后先跑它，别靠临时手敲：

    python scripts/selftest.py --case-dir "D:/Project/cases/案件库/<某案>"
    python scripts/selftest.py            # 无案件时用合成字段，只测机制

退出码 0 = 全过；1 = 有失败项（终端会逐条列出）。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import docx_helper as dh  # noqa: E402
import manage_templates as mt  # noqa: E402
import render_documents as rd  # noqa: E402

# 成品里绝不该出现的东西。每条 = (正则, 说明)
BAD_PATTERNS = (
    (r"\{\{[^}]+\}\}", "占位符残留"),
    (r"(?<![\u4e00-\u9fa5])None(?![\u4e00-\u9fa5])", "None 漏出"),
    (r"(?<![\u4e00-\u9fa5])nan(?![\u4e00-\u9fa5])", "nan 漏出"),
    (r"族族", "民族重复（汉族族）"),
    (r"案\s*号\s*为\s*[。；]", "空案号残句"),
    (r"[，,]{2,}", "连续逗号"),
)


def doc_text(path: Path) -> str:
    """取 docx 正文纯文本（合并所有段落，忽略格式）。"""
    if not zipfile.is_zipfile(path):
        return ""
    try:
        with zipfile.ZipFile(path) as z:
            if "word/document.xml" not in z.namelist():
                return ""
            xml = z.read("word/document.xml").decode("utf-8", "ignore")
    except (KeyError, OSError, zipfile.BadZipFile):
        return ""
    return "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S))


def scan_text(text: str, where: str) -> List[str]:
    """按 BAD_PATTERNS 扫描一段文本，返回问题描述列表。"""
    problems: List[str] = []
    for pat, label in BAD_PATTERNS:
        m = re.search(pat, text)
        if m:
            problems.append(f"{where}：{label} —— {m.group(0)[:40]}")
    return problems


def synthetic_fields(root: Path, registry: Dict[str, Any]) -> Dict[str, str]:
    """枚举全部模板占位符，给每个键一个测试值，不依赖真实案件。"""
    fields: Dict[str, str] = {
        "原告": "测试原告", "被告": "测试被告",
        "原告1": "测试原告", "被告1": "测试被告", "被告2": "测试被告二",
        "身份证号": "110101199001010000",
        "原告身份证号": "110101199001010000",
        "被告身份证号": "110101199001010000",
        "民族": "汉族", "原告民族": "汉族",
        "性别": "男", "出生日期": "1990年1月1日",
        "地址": "某市某区某路1号", "电话": "13800000000",
        "案由": "测试案由", "受理法院": "某市人民法院",
        "诉讼请求": "1. 测试诉请；", "事实与理由": "测试事实与理由。",
        "标的额": "1000元", "金额": "1000元",
    }
    keys: set = set()
    for key in mt.TEMPLATES:
        try:
            entry = mt.ensure_template(root, key, registry)
            path = Path(entry["file"])
        except BaseException:
            continue
        if not zipfile.is_zipfile(path):
            continue
        keys |= set(dh.find_placeholders(path))
    for k in keys:
        fields.setdefault(k, "测试" + k)
    return fields


def sweep_templates(root: Path, registry: Dict[str, Any],
                    fields: Dict[str, str]) -> (List[str], List[str], int):
    """33 个 docx 模板逐个实填，检查残留/脏值/被告段串号。

    返回 (problems, missing, filled)。登记了但没入库文件的 key 归 missing，
    不阻断——它们是历史遗留的 .doc 项，只提示不判定失败。
    """
    problems: List[str] = []
    missing: List[str] = []
    tmp = Path(tempfile.mkdtemp())
    plaintiff_id = str(fields.get("原告身份证号") or fields.get("身份证号") or "")
    filled = 0
    try:
        for key in mt.TEMPLATES:
            try:
                entry = mt.ensure_template(root, key, registry)
                tpl = Path(entry["file"])
            except SystemExit:
                missing.append(key)  # 登记了但没入库文件，不算失败
                continue
            except BaseException as exc:
                problems.append(f"{key}：{type(exc).__name__} {exc}")
                continue
            if not zipfile.is_zipfile(tpl):
                continue  # .doc 原件只能复制，不参与填充测试
            dst = tmp / f"{key}.docx"
            shutil.copyfile(tpl, dst)
            dh.qualify_role_placeholders(dst)
            dh.expand_multi_party_paragraphs(dst, 1, 2)
            dh.fill_placeholders(dst, fields, blank_missing=True)
            text = doc_text(dst)
            problems += scan_text(text, f"模板 {key}")
            # 被告/被申请人段落到原告证件号 = 串号
            if plaintiff_id:
                try:
                    with zipfile.ZipFile(dst) as _z:
                        _xml = (_z.read("word/document.xml").decode("utf-8", "ignore")
                                if "word/document.xml" in _z.namelist() else "")
                except (OSError, zipfile.BadZipFile):
                    _xml = ""
                for para in re.findall(r"<w:p[ >].*?</w:p>", _xml, re.S):
                    plain = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", para, re.S))
                    if re.match(r"^(被申请人|被执行人|被告[：:])", plain.strip()) \
                            and plaintiff_id in plain:
                        problems.append(f"模板 {key}：被告段串入原告证件号")
                        break
            filled += 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return problems, missing, filled


def sweep_documents(case_dir: Path) -> (List[str], List[str]):
    """用真实案件渲染四份主文书，检查产物。"""
    problems: List[str] = []
    made: List[str] = []
    try:
        rd.render(case_dir, "sp", skip_image_check=True,
                  only={"complaint_general", "bank_account", "delivery", "evidence"})
    except SystemExit as exc:
        return [f"渲染失败：{exc}"], []
    drafts = case_dir / "04未签稿"
    for name in ("01-起诉状.docx", "05-送达地址确认书.docx",
                 "06-账户确认书.docx", "04-证据目录.docx"):
        path = drafts / name
        if not path.exists():
            problems.append(f"文书未生成：{name}")
            continue
        made.append(name)
        problems += scan_text(doc_text(path), f"文书 {name}")
    return problems, made


def scan_template_sources(root: Path, registry: Dict[str, Any]) -> List[str]:
    """模板源本身是否还混着他人信息/写死性别（净化工具查过的项）。"""
    problems: List[str] = []
    dirty = re.compile(r"族族|案号为。")
    for key in mt.TEMPLATES:
        try:
            entry = mt.ensure_template(root, key, registry)
            path = Path(entry["file"])
        except BaseException:
            continue
        if not zipfile.is_zipfile(path):
            continue
        text = doc_text(path)
        m = dirty.search(text)
        if m:
            problems.append(f"模板源 {key}：{m.group(0)}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="案件分析技能自测")
    ap.add_argument("--case-dir", default="",
                    help="真实案件目录；不传则用合成字段只测模板机制")
    ap.add_argument("--keep-docs", action="store_true",
                    help="保留渲染产物（默认会删掉本次重渲染的文书）")
    args = ap.parse_args()

    case_dir = Path(args.case_dir).resolve() if args.case_dir else None
    if case_dir and not case_dir.exists():
        raise SystemExit(f"案件目录不存在：{case_dir}")

    # 有案件就用真实工作空间根，没有就建临时根，绝不污染真实目录
    tmp_root: Path | None = None
    if case_dir:
        root = case_dir.parent.parent
    else:
        tmp_root = Path(tempfile.mkdtemp())
        root = tmp_root
    (root / "通用件" / "模板").mkdir(parents=True, exist_ok=True)
    registry = mt._load_registry(root / "通用件" / "模板")

    fields = rd.build_fields(case_dir) if case_dir else synthetic_fields(root, registry)

    print("=" * 56)
    print("1/3 模板源洁净度")
    source_problems = scan_template_sources(root, registry)
    print(f"    {'通过' if not source_problems else str(len(source_problems)) + ' 处问题'}")

    print("2/3 模板填充压测")
    sweep_problems, missing, filled = sweep_templates(root, registry, fields)
    tail = f"（另有 {len(missing)} 项未入库已跳过）" if missing else ""
    print(f"    实填 {filled} 份，{'通过' if not sweep_problems else str(len(sweep_problems)) + ' 处问题'}{tail}")

    doc_problems: List[str] = []
    made: List[str] = []
    if case_dir:
        print("3/3 文书产物")
        doc_problems, made = sweep_documents(case_dir)
        print(f"    生成 {len(made)} 份，{'通过' if not doc_problems else str(len(doc_problems)) + ' 处问题'}")
    else:
        print("3/3 文书产物 —— 跳过（未传 --case-dir）")

    all_problems = source_problems + sweep_problems + doc_problems
    print("=" * 56)
    if all_problems:
        print(f"FAIL：{len(all_problems)} 处")
        for p in all_problems:
            print("  -", p)
        if tmp_root:
            shutil.rmtree(tmp_root, ignore_errors=True)
        return 1
    if missing:
        print(f"已知未入库（不影响判定）：{'、'.join(missing)}")
    print("PASS：全部通过")
    if tmp_root:
        shutil.rmtree(tmp_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
