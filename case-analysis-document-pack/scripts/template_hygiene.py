#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模板体检与净化：清掉实战模板里残留的他人案件信息、写死的性别民族和案由。

实战模板包来自真实用过的文书，常带着上一案的人名（写在横线上）、
写死的“男/女，X族”、以及不属于本案的具体案由。这些一旦被填充带进交付文书，
轻则张冠李戴，重则泄露他人信息。

用法：
    python template_hygiene.py --scan          # 只体检，列出问题
    python template_hygiene.py --fix           # 按内置规则修复源模板
    python template_hygiene.py --scan --root D:/Project/cases   # 体检已落地的副本

规则里只出现案由名与占位符，不写任何真实姓名——脚本本身也必须过敏感扫描。
"""
from __future__ import annotations

import argparse
import re
import shutil
import zipfile
from pathlib import Path
from typing import Dict, List, Tuple

SKILL_ROOT = Path(__file__).resolve().parent.parent
ASSETS = SKILL_ROOT / "templates"

# 横线上残留的人名：___张三___ -> 空横线，交回给律师手填。
# 日期填空（__年__月__日）落在同样的横线里，必须排除，否则会把日期清掉。
_DATE_CHARS = ("年", "月", "日", "时", "分", "元", "岁", "天", "个")

# 落款日期行统一格式：可选的“日期：/提交日期：”前缀 + 全角空格占位
SIGN_DATE = "　　　　年　　月　　日"
_DATE_LINE = re.compile(
    r"^(\s*(?:[^\s]{0,6}日期\s*[：:])?)[\s\u3000＿_]*年[\s\u3000＿_]*月[\s\u3000＿_]*日[\s\u3000＿_]*$")


def normalize_date_line(text: str) -> Tuple[str, bool] | None:
    """把各种落款日期写法统一成 `日期：　　　　年　　月　　日`。

    返回 (新文本, 是否需要右对齐)；不是落款日期行则返回 None。
    原来靠一长串空格顶到右边的情况，改成段落右对齐，不再用空格排版。
    """
    m = _DATE_LINE.match(text)
    if not m:
        return None
    prefix = m.group(1).strip()
    # “出生日期：   年   月   日”是当事人信息栏，不是落款，不能动
    if "出生" in prefix:
        return None
    if prefix and not prefix.endswith(("：", ":")):
        prefix += "："
    leading = len(text) - len(text.lstrip(" \u3000"))
    return prefix + SIGN_DATE, leading >= 6


def _blank_name(m: "re.Match") -> str:
    text = m.group(0)
    if any(c in text for c in _DATE_CHARS):
        return text
    return "__________"


# 规则：(正则, 替换, 说明)。顺序即执行顺序。
RULES: List[Tuple["re.Pattern", object, str]] = [
    (re.compile(r"_{2,}\s*[\u4e00-\u9fa5]{2,4}\s*_{2,}"), _blank_name, "横线上残留人名"),
    # 写死的性别与民族：男，汉族 -> {{性别}}，{{民族}}族
    (re.compile(r"[，,]\s*[男女]\s*[，,]\s*[\u4e00-\u9fa5]{1,3}族"), "，{{性别}}，{{民族}}族", "性别民族写死"),
    # “诉某人+具体案由”写成上一案的案子
    (re.compile(r"诉[\u4e00-\u9fa5]{2,4}(?:民间借贷|买卖合同|劳务合同|服务合同|"
                r"租赁合同|承揽合同|运输合同|借款合同|劳动争议)纠纷"),
     "诉{{被告}}{{案由}}", "写死他人案件"),
    # 紧跟占位符的具体案由，改成 {{案由}}
    (re.compile(r"(\{\{被告\}\}|\{\{公司名称\}\})\s*(?:\{\{公司名称\}\}\s*)?"
                r"(?:民间借贷|买卖合同|劳务合同|服务合同|租赁合同|承揽合同|运输合同|借款合同)纠纷"),
     r"\1{{案由}}", "案由写死"),
    # 上一案的开庭时间/法庭写死在正文里（要求中间带时间数字，避免修复后自匹配）
    (re.compile(r"定于\s*\{\{日期\}\}[^，。]{0,40}\d[^，。]{0,40}开庭审理"),
     "定于{{日期}}在{{受理法院}}开庭审理", "开庭时间地点写死"),
]

# 只有这些模板允许执行“案由改占位符”（要素式、专用文书本来就要写死案由）
_CAUSE_RULE_ALLOW = {
    "04-委托代理/授权委托书（立案用）.docx",
    "06-程序性申请/延期开庭申请书.docx",
    "08-情况说明/情况说明.docx",
    "08-情况说明/现金交付情况说明.docx",
    "08-情况说明/立案情况说明.docx",
}


def _read_doc_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode("utf-8", "ignore")


def _write_doc_xml(path: Path, xml: str) -> None:
    with zipfile.ZipFile(path) as z:
        items = [(i, z.read(i.filename)) for i in z.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in items:
            z.writestr(info, xml.encode("utf-8") if info.filename == "word/document.xml" else data)


def _iter_paras(xml: str):
    return re.findall(r"<w:p[ >](?:(?!</w:p>).)*?</w:p>", xml, re.S)


def _para_text(pxml: str) -> str:
    return "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", pxml, re.S))


def _rebuild_para(pxml: str, new_text: str) -> str:
    """把段落重建成单 run，保留 pPr 与一个非加粗 run 的 rPr。"""
    ppr = re.search(r"<w:pPr>.*?</w:pPr>", pxml, re.S)
    ppr = ppr.group(0) if ppr else ""
    runs = re.findall(r"<w:r(?:\s[^>]*)?>.*?</w:r>", pxml, re.S)
    rpr = ""
    for r in runs:
        if "<w:b " in r or "<w:b/" in r:
            continue
        rp = re.search(r"<w:rPr>.*?</w:rPr>", r, re.S)
        if rp:
            rpr = rp.group(0)
            break
    if not rpr and runs:
        rp = re.search(r"<w:rPr>.*?</w:rPr>", runs[0], re.S)
        rpr = rp.group(0) if rp else ""
    from docx_helper import _esc  # 复用转义，避免重复实现
    body = _esc(new_text).replace("\n", '</w:t><w:br/><w:t xml:space="preserve">')
    return f'<w:p>{ppr}<w:r>{rpr}<w:t xml:space="preserve">{body}</w:t></w:r></w:p>'


def scan_file(path: Path, rel: str) -> List[Dict[str, str]]:
    """返回该模板命中的问题列表。"""
    xml = _read_doc_xml(path)
    findings: List[Dict[str, str]] = []
    for pxml in _iter_paras(xml):
        text = _para_text(pxml)
        if not text.strip():
            continue
        norm = normalize_date_line(text)
        if norm and norm[0] != text:
            findings.append({"file": rel, "rule": "落款日期格式不统一",
                             "text": text.strip()[:70]})
            continue
        for idx, (pat, repl, desc) in enumerate(RULES):
            if idx == 3 and rel not in _CAUSE_RULE_ALLOW:
                continue
            m = pat.search(text)
            if not m:
                continue
            # 横线规则：命中日期填空不算问题（fix 时也不会改）
            if idx == 0 and any(c in m.group(0) for c in _DATE_CHARS):
                continue
            findings.append({"file": rel, "rule": desc,
                             "text": text.strip()[:70]})
            break
    return findings


def _set_jc_right(pxml: str) -> str:
    """把段落设为右对齐（替换已有 jc，或注入到 pPr）。"""
    if re.search(r'<w:jc\s+w:val="[^"]*"\s*/>', pxml):
        return re.sub(r'<w:jc\s+w:val="[^"]*"\s*/>', '<w:jc w:val="right"/>', pxml, count=1)
    if "<w:pPr>" in pxml:
        return pxml.replace("<w:pPr>", '<w:pPr><w:jc w:val="right"/>', 1)
    return pxml.replace("<w:p>", '<w:p><w:pPr><w:jc w:val="right"/></w:pPr>', 1)


def fix_file(path: Path, rel: str) -> int:
    """就地修复一个模板，返回改动段落数。"""
    xml = _read_doc_xml(path)
    changed = 0

    def do_para(m: "re.Match") -> str:
        nonlocal changed
        pxml = m.group(0)
        text = _para_text(pxml)
        if not text.strip():
            return pxml
        new = text
        for idx, (pat, repl, _desc) in enumerate(RULES):
            if idx == 3 and rel not in _CAUSE_RULE_ALLOW:
                continue
            new = pat.sub(repl, new)
        right_align = False
        norm = normalize_date_line(new)
        if norm:
            new, right_align = norm
        if new == text and not right_align:
            return pxml
        changed += 1
        rebuilt = _rebuild_para(pxml, new)
        return _set_jc_right(rebuilt) if right_align else rebuilt

    new_xml = re.sub(r"<w:p[ >](?:(?!</w:p>).)*?</w:p>", do_para, xml, flags=re.S)
    if changed:
        _write_doc_xml(path, new_xml)
    return changed


def list_templates(root: Path) -> List[Tuple[Path, str]]:
    base = root if root.exists() else ASSETS
    out = []
    for p in sorted(base.rglob("*.docx")):
        out.append((p, p.relative_to(base).as_posix()))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="模板体检/净化")
    ap.add_argument("--root", default="", help="模板根目录，默认用技能自带 templates")
    ap.add_argument("--scan", action="store_true", help="只体检")
    ap.add_argument("--fix", action="store_true", help="按规则修复")
    args = ap.parse_args()

    root = Path(args.root) if args.root else ASSETS
    total = 0
    for path, rel in list_templates(root):
        hits = scan_file(path, rel)
        if not hits:
            continue
        total += len(hits)
        print(f"[{rel}] {len(hits)} 处")
        for h in hits:
            print(f"    {h['rule']}: {h['text']}")
        if args.fix:
            n = fix_file(path, rel)
            print(f"    已修 {n} 段")
    print(f"\n合计 {total} 处问题" + ("，已修复" if args.fix else "（未改动，加 --fix 执行修复）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
