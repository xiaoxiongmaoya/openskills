#!/usr/bin/env python3
"""构建「民事起诉状（普通版）」通用模板。

来源：`_raw_doc/诉状模板(1)(1).doc`（技能目录内，已 gitignore；实为 docx 改后缀的空白表单）。
做法：保留原文件全部段落样式（仿宋、行距、缩进），逐段替换文字为 {{字段}} 占位符，
      不重建文档结构，避免版式漂移。

用法：
  python scripts/build_complaint_ordinary.py
  python scripts/build_complaint_ordinary.py --raw <原始文件> --out <输出文件>
"""
from __future__ import annotations

import argparse
import re
import shutil
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE.parent / "templates"
RAW_DIR = HERE.parent / "_raw_doc"
DEFAULT_RAW = RAW_DIR / "诉状模板(1)(1).doc"
DEFAULT_OUT = ASSETS / "民事起诉状（普通版）.docx"

PH = "{{%s}}"

# 段落序号 -> 新文本；None 表示清空内容（保留空行占样式）
LAYOUT = [
    "民事起诉状",
    f"原告：{PH % '原告'}{PH % '原告性别段'}{PH % '原告民族段'}{PH % '原告出生段'}"
    f"{PH % '原告地址段'}{PH % '原告证件段'}{PH % '原告电话段'}。",
    f"被告：{PH % '被告'}{PH % '被告性别段'}{PH % '被告民族段'}{PH % '被告出生段'}"
    f"{PH % '被告地址段'}{PH % '被告证件段'}{PH % '被告电话段'}。",
    f"案由：{PH % '案由'}",
    "诉讼请求：",
    PH % "诉讼请求",
    None,
    "事实与理由：",
    PH % "事实与理由",
    None,
    None,
    "此致",
    PH % "受理法院",
    "具状人（签字/捺印）：",
    "   年  月  日",
    None,
    None,
]

RIGHT_ALIGN = {13, 14}  # 具状人与日期靠右

PARA = re.compile(r"<w:p(?:\s[^>]*)?>.*?</w:p>|<w:p(?:\s[^>]*)?/>", re.S)


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _first_rpr(para: str) -> str:
    m = re.search(r"<w:r(?:\s[^>]*)?>\s*(<w:rPr>.*?</w:rPr>)", para, re.S)
    return m.group(1) if m else ""


def _rewrite(para: str, text: str, right: bool) -> str:
    """用该段原有段落样式 + 第一个 run 的字体，写一段新文字。"""
    ppr_m = re.search(r"<w:pPr>.*?</w:pPr>", para, re.S)
    ppr = ppr_m.group(0) if ppr_m else ""
    if right:
        ppr = re.sub(r"<w:jc [^>]*/>", "", ppr)
        ppr = ppr.replace("<w:pPr>", '<w:pPr><w:jc w:val="right"/>', 1) if ppr else '<w:pPr><w:jc w:val="right"/></w:pPr>'
    rpr = _first_rpr(para)
    runs = []
    for idx, line in enumerate(str(text or "").split("\n")):
        br = "<w:br/>" if idx else ""
        runs.append(f'<w:r>{rpr}{br}<w:t xml:space="preserve">{_esc(line)}</w:t></w:r>')
    return f"<w:p>{ppr}{''.join(runs)}</w:p>"


def _rebuild_document(xml: str) -> str:
    index = [0]

    def _one(m: re.Match) -> str:
        i = index[0]
        index[0] += 1
        if i >= len(LAYOUT):
            raise SystemExit(f"段落数超出布局定义：第 {i + 1} 段；请先核对源文件")
        text = LAYOUT[i]
        return _rewrite(m.group(0), "" if text is None else text, i in RIGHT_ALIGN)

    out = PARA.sub(_one, xml)
    if index[0] != len(LAYOUT):
        raise SystemExit(f"段落数不匹配：源文件 {index[0]} 段，布局定义 {len(LAYOUT)} 段")
    return out


def build(raw: Path, out: Path) -> Path:
    if not raw.is_file():
        raise SystemExit(f"原始模板不存在：{raw}")
    if not zipfile.is_zipfile(raw):
        raise SystemExit(f"不是 docx 包（疑似真二进制 .doc，无法处理）：{raw}")
    with zipfile.ZipFile(raw) as z:
        items = [(info, z.read(info.filename)) for info in z.infolist()]
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        try:
            out.chmod(0o666)
        except OSError:
            pass
    tmp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info, data in items:
            if info.filename == "word/document.xml":
                data = _rebuild_document(data.decode("utf-8")).encode("utf-8")
            zout.writestr(info, data)
    shutil.move(str(tmp), str(out))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="构建普通版民事起诉状模板")
    ap.add_argument("--raw", default=str(DEFAULT_RAW))
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    print(f"已生成：{build(Path(args.raw), Path(args.out))}")


if __name__ == "__main__":
    main()
